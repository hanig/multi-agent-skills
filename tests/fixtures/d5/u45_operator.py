"""Synthetic activation/crashes around the real merge operator script entry."""
from contextlib import nullcontext
import atexit
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests import test_d5_submission as t
import d5_activation as activation
import merge_unit as merge_operator
import d5_merge_journal as J


def probe(directory):
    fd = os.open(Path(directory) / 'authority.drain.lock', os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'blocked'
        return 'available'
    finally:
        os.close(fd)


def main():
    data = json.loads(Path(sys.argv[1]).read_text())
    mode = sys.argv[2]
    if mode == 'hold':
        with t.protocol.pin(data['record']) as pinned, pinned.drain():
            print('READY', flush=True)
            sys.stdin.readline()
        return
    with ExitStack() as stack:
        stack.enter_context(nullcontext())
        stack.enter_context(mock.patch.object(merge_operator, 'merge_launch_admitted', return_value=True))
        stack.enter_context(nullcontext())
        stack.enter_context(mock.patch.object(t.registry.CP, '_external_base', return_value=Path(data['external'])))
        stack.enter_context(mock.patch.object(t.host, 'session_anchor', return_value=('native', t.ANCHOR)))
        stack.enter_context(mock.patch.object(t.admission.time, 'time', return_value=150 if mode != 'recover' else 300))
        write, complete = J._write, J.Capability.complete
        communicate = subprocess.Popen.communicate
        def writing(handle, row):
            if mode == 'receipt' and row['d5']['state'] == 'Recorded':
                raise OSError('injected durable receipt before intent update')
            result = write(handle, row)
            if mode == 'prepare' and row['d5']['state'] == 'Bound':
                os._exit(71)
            return result
        def finishing(process, *args, **kwargs):
            result = communicate(process, *args, **kwargs)
            if mode == 'dispatch' and process.args[:3] == ['gh', 'pr', 'merge']:
                os._exit(72)
            return result
        def completing(capability, outcome):
            if mode == 'observed':
                os._exit(73)
            return complete(capability, outcome)
        stack.enter_context(mock.patch.object(J, '_write', writing))
        stack.enter_context(mock.patch.object(J.Capability, 'complete', completing))
        stack.enter_context(mock.patch.object(subprocess.Popen, 'communicate', finishing))
        if mode == 'receipt':
            # Read-only atexit measurement: never close the retained holder.
            atexit.register(lambda: print('before-process-exit DRAIN=' + probe(data['record']), flush=True))
        sys.argv = [str(ROOT / 'skills/hanig-orchestrate/scripts/merge_unit.py')] + data['argv']
        runpy.run_path(sys.argv[0], run_name='__main__')


if __name__ == '__main__':
    main()
