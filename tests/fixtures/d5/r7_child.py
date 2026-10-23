"""Test-only fresh process and explicit boundary; never sends a real effect."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import test_d5_submission as t
import d5_effect_child as child


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)


def wait_for(word):
    if sys.stdin.readline().strip() != word:
        raise RuntimeError("test control closed or unexpected: " + word)


def effect(data, inherited, mode):
    directory = Path(data['directory'])
    calls, order = [], []
    real_submit, real_pin = t.submission.submit, t.protocol.pin
    local_os = mock.Mock(wraps=os)
    def noninherit(fd, flag):
        order.append('set_inheritable')
        return os.set_inheritable(fd, flag)
    local_os.set_inheritable.side_effect = noninherit

    @contextmanager
    def pin(path):
        order.append('pin')
        assert order[0] == 'set_inheritable', order
        assert not os.get_inheritable(inherited)
        with real_pin(path) as record:
            yield record

    def submit(*args, **kwargs):
        assert not any(rank > 0 for _, rank, _, _ in t.protocol.locks._held())
        observed = {}
        for fd in data.get('inspect_fds', []):
            try:
                info = os.fstat(fd)
                observed[str(fd)] = [info.st_dev, info.st_ino]
            except OSError:
                observed[str(fd)] = None
        emit('adopted', inheritable=os.get_inheritable(inherited), pid=os.getpid(),
             observed=observed)
        wait_for('request')
        return real_submit(*args, **kwargs)

    class Boundary(t.submission.Boundary):
        def start(self, operation):
            calls.append('start')
            if mode == 'start-error':
                raise RuntimeError('start error')
            return {'not-submitted': t.submission.Initiation.NOT_SUBMITTED,
                    'unknown': t.submission.Initiation.UNCERTAIN}.get(
                        mode, t.submission.Initiation.SUBMITTED)

        def finish(self):
            calls.append('finish')
            emit('waiting')
            wait_for('finish')
            if mode == 'finish-error':
                raise RuntimeError('finish error')
            if mode == 'inactive-completion':
                active.return_value = False
            return dict(t.OK)

    # Activation and measured identity/time patches exist in this fixture only.
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(t.registry.CP, '_external_base',
                                              return_value=Path(data['external'])))
        active = stack.enter_context(mock.patch.object(t.registry, 'd5_active',
                                                     return_value=mode != 'inactive'))
        stack.enter_context(mock.patch.object(t.host, 'session_anchor',
                                              return_value=('native', t.ANCHOR)))
        stack.enter_context(mock.patch.object(t.admission.time, 'time', return_value=150))
        stack.enter_context(mock.patch.object(child, 'os', local_os))
        stack.enter_context(mock.patch.object(t.submission, 'submit', side_effect=submit))
        stack.enter_context(mock.patch.object(t.protocol, 'pin', pin))
        if mode == 'accounting-error':
            stack.enter_context(mock.patch.object(t.completion._Completion, 'complete',
                side_effect=t.ledger.EffectError(t.ledger.DURABILITY)))
        if mode == 'close-error':
            local_os.close.side_effect = OSError('adopted close error')
        # Observe R6 epilogue completion and pause just before original close.
        def close(fd):
            emit('closing', retained=True)
            wait_for('close')
            os.close(fd)
        if mode == 'close-barrier':
            local_os.close.side_effect = close
        try:
            result = child.bootstrap(inherited, directory, *data['args'],
                **dict(data['request'], anchor=t.ANCHOR),
                operation=t.submission.Operation('child', 'fixture',
                    data['scope'].encode(), b'child operation'), boundary=Boundary())
            error, initiation = None, result.initiation.value
        except BaseException as exc:
            error, initiation = str(exc), None
        try:
            os.fstat(inherited)
            retained = True
        except OSError:
            retained = False
        emit('returned', error=error, initiation=initiation, retained=retained,
             calls=calls, order=order)
        wait_for('exit')


def ancestor(data, death_fd, mode):
    # R8 will own production launch. This is the specified test-only launch.
    with t.protocol.pin(data['directory']) as record, record.drain() as drain:
        subprocess.Popen([sys.executable, __file__, 'child', json.dumps(data),
                          str(drain), mode], stdin=sys.stdin, stdout=sys.stdout,
                         stderr=sys.stderr, pass_fds=(drain,), close_fds=True)
        os.read(death_fd, 1)
        os._exit(0)  # Ancestor dies with its DRAIN reference still open.


if __name__ == '__main__':
    action, raw, descriptor, mode = sys.argv[1:]
    data = json.loads(raw)
    (ancestor if action == 'ancestor' else effect)(data, int(descriptor), mode)
