"""cmd_merge observable effects, frozen base versus candidate; isolated process."""
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'skills/hanig-swarm/scripts'))
import swarm
import d5_activation


def capture(version, case):
  module = swarm
  if version == 'base':
    raw = subprocess.check_output(['git', 'show',
      '276e76af05a83846ea9c45ae7d030cf11a2e4e94:skills/hanig-swarm/scripts/swarm.py'], cwd=ROOT)
    module = types.ModuleType('r4_base')
    module.__file__ = swarm.__file__
    exec(compile(raw, module.__file__, 'exec'), module.__dict__)
  with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()
    repo, state = root / 'repo', root / 'state'
    subprocess.run(['git', 'init', str(repo)], capture_output=True, check=True)
    state.mkdir()
    if case == 'corrupt':
      (state / swarm.SE.MERGE_RECEIPTS).write_bytes(b'bad\n')
    args = argparse.Namespace(state_dir=None if case == 'active' else str(state),
      unit='u', repo='r', pr='1', target='main', head='a'*40, merged_as='b'*40,
      method='invalid' if case == 'invalid' else 'squash',
      target_commit='c'*40, integration_status='fixture')
    def snapshot():
      return {str(p.relative_to(root)): p.read_bytes().hex() if p.is_file() else None
          for p in root.rglob('*')}
    before, events, enabled = snapshot(), [], False
    def audit(event, values):
      if enabled:
        if event == 'subprocess.Popen':
          events.append(['argv', values[1]])
        elif event == 'open' and values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
          events.append(['write-open', str(values[0])])
        elif event in ('os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod'):
          events.append([event, str(values[0])])
    sys.addaudithook(audit)
    os.chdir(repo)
    with patch.object(d5_activation, 'd5_active', return_value=case == 'active'), \
        patch.object(module.CP, '_external_base', return_value=root / 'external'), \
        patch.object(module.time, 'strftime', return_value='fixture-time'), \
        patch.dict(os.environ, {'USER': 'fixture'}), \
        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
      enabled = True
      try:
        result = module.cmd_merge(args)
      finally:
        enabled = False
        os.chdir(ROOT)
    after = snapshot()
    data = dict(result=result, events=events, removed=sorted(set(before) - set(after)),
      changed={k: v for k, v in after.items() if k not in before or before[k] != v})
    return json.loads(json.dumps(data).replace(str(root), '<root>'))


if __name__ == '__main__':
  print(json.dumps(capture(*sys.argv[1:]), sort_keys=True))
