"""Pipe-driven lock peer. Deadlines belong to the test parent, never the lock."""
import os
from pathlib import Path
import sys
from unittest import mock

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
        elif action == "permissions":
            old_mask = os.umask(0o277)
            try:
                for case in ("new", "persisted", "concurrent", "repaired"):
                    for rank, name in locks._NAMES.items():
                        path = Path(directory) / name
                        if case == "persisted":
                            path.chmod(0o400)  # prior implementation's durable residue
                        elif path.exists():
                            path.unlink()  # fixture setup only
                        inode = path.stat().st_ino if path.exists() else None
                        real_open = os.open
                        pending = [case in ("concurrent", "repaired")]

                        def opening(*args, **kwargs):
                            if pending and pending.pop():
                                # Another creator publishes 0400 after our stat.
                                os.close(real_open(*args, **kwargs))
                                if case == "repaired":
                                    path.chmod(0o600)
                                    raise PermissionError("creator fixed mode after EACCES")
                            return real_open(*args, **kwargs)

                        with mock.patch.object(os, "open", side_effect=opening):
                            guard = (record.drain, record.start, record.state)[rank]
                            with guard():
                                assert path.stat().st_mode & 0o777 == 0o600, case
                                if inode is not None:
                                    assert path.stat().st_ino == inode
                            with guard():
                                pass
            finally:
                os.umask(old_mask)
            print("permissions repaired", flush=True)
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
