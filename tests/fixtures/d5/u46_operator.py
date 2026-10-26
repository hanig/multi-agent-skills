"""Faults and read-only lifetime measurements around the actual operator CLI.

Each invocation exits after one submit; no recovery, retry or descriptor release
is added to a live submit. All external commands use the caller's local fixtures.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.fixtures.d5 import u45_operator as operator
from tests.test_d5_u4_2_request_authority import tree_bytes
import d5_merge_journal as J
import d5_submission as DS
import merge_unit as M
import swarm_state as state


def main():
    data = json.loads(Path(sys.argv[1]).read_text())
    mode = sys.argv[2]
    # Mutations are applied before capturing the original operator methods.
    if len(sys.argv) == 4:
        from tests.fixtures.d5.u46_mutations import patch
        with patch(sys.argv.pop()):
            return main()
    facts = dict(cancel_refused=False, cancel_unchanged=False, completion=[],
                 writes=[], loss_detected=False, writes_after_loss=[])
    submit, start, observe = DS.submit, M._MergeBoundary.start, M._MergeBoundary.observe
    communicate, write, complete = subprocess.Popen.communicate, J._write, J.Capability.complete
    holds = state.holds_lease
    dispatched = []
    injected = []
    def launching(boundary, operation):
        if mode == 'not-submitted':
            return DS.Initiation.NOT_SUBMITTED
        result = start(boundary, operation)
        dispatched.append(boundary.process)
        if mode == 'cancel-after-dispatch':
            path = Path(boundary.state_dir) / ('merge-unit-' + operation.request_id + '.json')
            before = tree_bytes(boundary.state_dir)
            try:
                M.cancel_before_request(path, M.read_object(path), None, 'interrupted after launch')
            except M.Refusal:
                facts['cancel_refused'] = True
            facts['cancel_unchanged'] = tree_bytes(boundary.state_dir) == before
            raise KeyboardInterrupt('synthetic post-dispatch cancellation')
        return result
    def finishing(process, *args, **kwargs):
        result = communicate(process, *args, **kwargs)
        if process in dispatched and not injected:
            injected.append(True)
            if mode == 'timeout':
                raise subprocess.TimeoutExpired(process.args, 300)
            if mode == 'interrupt':
                raise KeyboardInterrupt('synthetic interrupted response')
            if mode == 'lease-loss':
                facts['loss_detected'] = True
        return result
    def detecting(directory):
        return False if facts['loss_detected'] else holds(directory)
    def writing(handle, row):
        facts['writes'].append(row['d5']['state'])
        if facts['loss_detected']:
            facts['writes_after_loss'].append(row['d5']['state'])
        return write(handle, row)
    def completing(capability, outcome):
        result = complete(capability, outcome)
        facts['completion'].append(dict(drain=operator.probe(data['record']),
                                         lease=holds(capability.handle.state_dir)))
        return result
    def observing(boundary, operation):
        if mode == 'pre-dispatch':
            raise subprocess.TimeoutExpired(boundary.cmd['target'], 60)
        return observe(boundary, operation)
    def submitting(*args, **kwargs):
        try:
            if mode == 'pre-start':
                with mock.patch.object(DS.admission.time, 'time', return_value=201):
                    return submit(*args, **kwargs)
            return submit(*args, **kwargs)
        finally:
            facts.update(drain=operator.probe(data['record']), lease=holds(args[2]),
                         reaped=bool(dispatched) and all(p.poll() is not None for p in dispatched))
            print('LIFETIME ' + json.dumps(facts), flush=True)
    with ExitStack() as stack:
        for obj, name, replacement in ((DS, 'submit', submitting),
                (M._MergeBoundary, 'start', launching), (M._MergeBoundary, 'observe', observing),
                (subprocess.Popen, 'communicate', finishing), (state, 'holds_lease', detecting),
                (J, '_write', writing), (J.Capability, 'complete', completing)):
            stack.enter_context(mock.patch.object(obj, name, replacement))
        try:
            operator.main()
        except KeyboardInterrupt:
            raise SystemExit(130)


if __name__ == '__main__':
    main()
