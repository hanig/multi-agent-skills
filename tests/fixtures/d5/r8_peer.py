"""R8 test children: local setup, ancestor, worker and real R3 installation."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import test_d5_submission as t
import d5_process as process
import d5_install as install
from tests.fixtures.d5.r7_child import emit, wait_for

BOOTSTRAP = str(Path(__file__).with_name('r7_child.py'))


def setup(root):
    root = Path(root)
    repo, state = root / 'repo', root / 'state'
    subprocess.run(['git', 'init', str(repo)], capture_output=True,
                   check=True, timeout=20)
    state.mkdir(mode=0o700)
    (repo / 'docs').mkdir()
    (repo / 'docs/orchestrator-mandate.md').write_bytes(t.MANDATE)
    args = [str(repo), 'p', str(state)]
    scope = dict(plans=['p'], repositories=['r'], actions=['write'], trackers=['t'])
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(t.registry.CP, '_external_base',
                                              return_value=root / 'external'))
        stack.enter_context(mock.patch.object(t.registry, 'd5_active', return_value=True))
        stack.enter_context(mock.patch.object(t.host, 'session_anchor',
                                              return_value=('native', t.ANCHOR)))
        entry = t.registry.register(*args)
        t.lease.grant(*args, issued_at=100, confirmed_at=100, expires_at=200,
            mandate_bytes=t.MANDATE, scope=scope, owner_response='fixture',
            displayed_bounds='fixture')
        original = t.store.claim(entry['record_dir'], 101, t.MANDATE, 'native',
                                 t.ANCHOR, lambda _: 'alive')
        emit('setup', data=dict(args=args, directory=entry['record_dir'],
            scope=json.dumps(scope), external=str(root / 'external'),
            request=dict(grant_id=original['grant_id'], session_id='native', generation=1)))


def ancestor(raw):
    data = json.loads(raw)
    with t.protocol.pin(data['directory']) as record, record.drain() as fd:
        with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])):
            proc = process.protected_spawn(data['directory'], fd, BOOTSTRAP,
                ['child', raw, str(fd), 'close-barrier'],
                stdin=None, stdout=None, stderr=None)
        proc.wait(timeout=60)


def worker(raw):
    observed = {}
    for fd in json.loads(raw):
        try:
            info = os.fstat(fd)
            observed[str(fd)] = [info.st_dev, info.st_ino]
        except OSError:
            observed[str(fd)] = None
    emit('ready', observed=observed)
    wait_for('exit')


def standard_slots(root):
    from tests.test_d5_process import descriptors
    with t.protocol.pin(root) as record, record.drain() as fd:
        for standard in (0, 1, 2):
            saved = os.dup(standard)
            try:
                os.dup2(fd, standard)
                before = descriptors()
                with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])), \
                        mock.patch.object(process.subprocess, 'Popen') as popen:
                    try:
                        process.protected_spawn(root, standard, BOOTSTRAP)
                        error = None
                    except process.AuthorityError as exc:
                        error = str(exc)
                    spawned = popen.called
                    stream = ('stdin', 'stdout', 'stderr')[standard]
                    try:
                        process.protected_spawn(root, fd, BOOTSTRAP, **{stream: None})
                        implicit = None
                    except process.AuthorityError as exc:
                        implicit = str(exc)
                    assert not popen.called, 'inherited protocol descriptor'
                    os.set_inheritable(standard, False)
                    process.protected_spawn(root, fd, BOOTSTRAP, **{stream: None})
                    popen.assert_called_once()
                    if standard == 1:
                        with t.unittest.TestCase().assertRaises(process.AuthorityError):
                            process.protected_spawn(root, fd, BOOTSTRAP, stdout=None, stderr=subprocess.STDOUT)
                unchanged = descriptors() == before
            finally:
                os.dup2(saved, standard)
                os.close(saved)
            emit('standard-slot', standard=standard, error=error,
                 spawned=spawned, unchanged=unchanged, implicit=implicit)
            saved = os.dup(standard)
            read, write = os.pipe()
            try:
                os.dup2(read, standard)
                with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])), \
                        mock.patch.object(process.subprocess, 'Popen') as popen:
                    try:
                        process.protected_spawn(root, fd, BOOTSTRAP, pass_fds=(standard,))
                        error = None
                    except process.AuthorityError as exc:
                        error = str(exc)
                    assert not popen.called, 'overwrote control descriptor'
                    stream = ('stdin', 'stdout', 'stderr')[standard]
                    process.protected_spawn(root, fd, BOOTSTRAP, pass_fds=(standard,), **{stream: None})
                    popen.assert_called_once()
                    if standard == 2:
                        process.protected_spawn(root, fd, BOOTSTRAP, pass_fds=(2,), stdout=2, stderr=subprocess.STDOUT)
                        assert popen.call_count == 2
            finally:
                os.dup2(saved, standard)
                for opened in (saved, read, write):
                    os.close(opened)
            emit('control-slot', error=error)


def closed_streams(root, low_first=False):
    from tests.test_d5_process import TestProcess
    case = TestProcess()
    case.setUp()
    try:
        case.setup_effect()
        if not low_first:
            _, fd = case.holding()
        saved = [(n, os.dup(n)) for n in (0, 2)]
        try:
            os.close(0)
            os.close(2)
            if low_first:
                _, low = case.holding()
                fd = os.dup(low)
                assert low == 2 and fd > 2 and not os.get_inheritable(low)
            proc = case.launch(fd, stderr=None)
        finally:
            if low_first:
                os.close(fd)
                case.stack.close()
            for target, copy in saved:
                os.dup2(copy, target)
                os.close(copy)
        case.event(proc, 'adopted')
        case.stack.close()
        case.assertEqual(case.probe(), 'blocked')
        case.complete_effect(proc)
        case.assertEqual(case.probe(), 'available')
        case.send(proc, 'exit')
        stdout, stderr = proc.communicate(timeout=20)
        case.assertEqual((proc.returncode, stdout, stderr), (0, b'', None))
        _, fd = case.holding()
        for stream in ('stdin', 'stdout', 'stderr'):
            saved = [(n, os.dup(n)) for n in (0, 1, 2)]
            try:
                for target, _ in saved:
                    os.close(target)
                with mock.patch.object(process, 'DESIGNATED_BOOTSTRAPS', frozenset([BOOTSTRAP])), \
                        mock.patch.object(process.subprocess, 'Popen') as popen:
                    process.protected_spawn(case.directory, fd, BOOTSTRAP, **{stream: None})
                    popen.assert_called_once()
            finally:
                for target, copy in saved:
                    os.dup2(copy, target)
                    os.close(copy)
    finally:
        case.doCleanups()
    emit('closed-streams-completed')


def installer(raw):
    directory = Path(json.loads(raw)['directory'])
    record = t.store.read(directory)
    record['grant_id'] = 'b' * 32
    flock, replace = t.protocol.locks.fcntl.flock, t.store.replace
    identity = (directory / 'authority.drain.lock').stat()

    def observed(fd, mode):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            try:
                flock(fd, mode | t.protocol.locks.fcntl.LOCK_NB)
            except BlockingIOError:
                emit('exclusive-probe', blocked=True)
            else:
                emit('exclusive-probe', blocked=False)
                return
        return flock(fd, mode)

    def replacing(*args):
        rows = [json.loads(p.read_bytes()) for p in
                (directory / t.ledger.DIRECTORY).glob('*.json')]
        assert len(rows) == 1 and rows[0]['state'] == 'completed', rows
        return replace(*args)

    with mock.patch.object(t.registry, 'd5_active', return_value=True), \
            mock.patch.object(t.host, 'session_anchor', return_value=('native', t.ANCHOR)), \
            mock.patch.object(t.protocol.locks.fcntl, 'flock', side_effect=observed), \
            mock.patch.object(t.store, 'replace', side_effect=replacing):
        install.replace_authority(directory, record)
    emit('installed')


if __name__ == '__main__':
    {'setup': setup, 'ancestor': ancestor, 'worker': worker,
     'installer': installer, 'standard-slots': standard_slots,
     'closed-streams': closed_streams,
     'closed-streams-low': lambda root: closed_streams(root, True)}[sys.argv[1]](sys.argv[2])
