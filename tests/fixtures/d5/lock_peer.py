"""Pipe-driven lock peer. Deadlines belong to the test parent, never the lock."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] /
                       "skills/hanig-orchestrate/scripts"))
import d5_locks as locks


def main():
    action, directory = sys.argv[1:3]
    with locks.pin(directory) as record:
        if action == "nested":
            calls = []
            flock = locks.fcntl.flock

            def counted(fd, mode):
                calls.append(mode)
                flock(fd, mode)

            locks.fcntl.flock = counted
            with record.drain():
                with record.start(), record.state():
                    with locks.pin(directory) as again:
                        with again.drain(), again.start(), again.state():
                            pass
            print(calls, flush=True)
        elif action == "inherited":
            inherited = int(sys.argv[3])

            def forbidden(*args):
                raise AssertionError("import must not flock an inherited description")

            locks.fcntl.flock = forbidden
            with record.drain(inherited_fd=inherited):
                os.close(inherited)
                print("held", flush=True)
                assert sys.stdin.readline() == "release\n"
            print("released", flush=True)
        elif action == "waiter":
            flock = locks.fcntl.flock

            def announced(fd, mode):
                print("waiting", flush=True)
                flock(fd, mode)

            locks.fcntl.flock = announced
            with record.drain(shared=False):
                print("acquired", flush=True)
                assert sys.stdin.readline() == "release\n"
        else:
            raise AssertionError(action)


if __name__ == "__main__":
    main()
