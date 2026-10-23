"""Finite real-process fixtures for R8a transport tests."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    action, *args = sys.argv[1:]
    if action == 'inspect':
        print(json.dumps(dict(cwd=os.getcwd(), env=dict(os.environ),
                              session=os.getsid(0), pid=os.getpid(), args=args)))
        print('error', file=sys.stderr)
    elif action == 'barrier':
        ready, release = map(Path, args)
        ready.touch()
        deadline = time.monotonic() + 5
        while not release.exists():
            if time.monotonic() > deadline:
                return 9
            time.sleep(.01)
        print('released')
    elif action == 'flood':
        for _ in range(128):
            os.write(1, b'o' * 16384)
            os.write(2, b'e' * 16384)
        Path(args[0]).touch()
    elif action == 'echo':
        data = sys.stdin.buffer.read()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(b'error')
    elif action == 'fd':
        try:
            stat = os.fstat(int(args[0]))
            print(json.dumps([stat.st_dev, stat.st_ino]))
        except OSError:
            print('null')
    elif action == 'group':
        pid = os.fork()
        if pid == 0:
            with open(args[1], 'wb', buffering=0) as pipe:
                pipe.write(b'r')
                Path(args[0]).touch()
                while True:
                    time.sleep(1)
        while True:
            time.sleep(1)
    elif action == 'mark':
        Path(args[0]).write_text('unexpected launch')
    return 0


if __name__ == '__main__':
    sys.exit(main())
