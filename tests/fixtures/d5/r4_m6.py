"""Unit-local observable-effects capture of cmd_merge against the frozen base."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'skills/hanig-swarm/scripts'))
import swarm
import d5_activation

BASE = '276e76af05a83846ea9c45ae7d030cf11a2e4e94'


def capture(version, case):
    module = swarm
    if version == 'base':
        raw = subprocess.check_output(['git', 'show', BASE + ':skills/hanig-swarm/scripts/swarm.py'], cwd=str(ROOT))
        module = types.ModuleType('r4_pinned_swarm')
        module.__file__ = swarm.__file__
        exec(compile(raw, module.__file__, 'exec'), module.__dict__)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        subprocess.run(['git', 'init', str(repo)], capture_output=True, check=True)
        state = root / 'state'
        state.mkdir()
        if case == 'corrupt':
            (state / swarm.SE.MERGE_RECEIPTS).write_bytes(b'bad\n')
        args = argparse.Namespace(state_dir=str(state), unit='u', repo='r', pr='1',
            target='main', head='a'*40, merged_as='b'*40, method='squash',
            target_commit='c'*40, integration_status='fixture')
        if case == 'invalid':
            args.method = 'invalid'
        if case == 'active':
            args.state_dir = None  # Exercise the potentially mutating default preparation.
        def snapshot():
            return {str(p.relative_to(root)): p.read_bytes().hex() if p.is_file() else None
                    for p in root.rglob('*')}
        before = snapshot()
        events, enabled = [], [False]
        def audit(event, values):
            if not enabled[0]:
                return
            if event == 'subprocess.Popen':
                events.append(['argv', values[1]])
            elif event == 'open' and values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                events.append(['write-open', str(values[0])])
            elif event in ('os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod'):
                events.append([event, str(values[0])])
        sys.addaudithook(audit)
        cwd = os.getcwd()
        try:
            os.chdir(repo)
            with mock.patch.object(d5_activation, 'd5_active', return_value=case == 'active'), \
                    mock.patch.object(module.CP, '_external_base', return_value=root / 'external'), \
                    mock.patch.object(module.time, 'strftime', return_value='fixture-time'), \
                    mock.patch.dict(os.environ, {'USER': 'fixture'}), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                enabled[0] = True
                try:
                    result = module.cmd_merge(args)
                finally:
                    enabled[0] = False
        finally:
            os.chdir(cwd)
        after = snapshot()
        changed = {k: v for k, v in after.items() if k not in before or before[k] != v}
        removed = sorted(set(before) - set(after))
        data = dict(result=result, events=events, changed=changed, removed=removed)
        return json.loads(json.dumps(data).replace(str(root), '<root>'))


if __name__ == '__main__':
    print(json.dumps(capture(*sys.argv[1:]), sort_keys=True))
