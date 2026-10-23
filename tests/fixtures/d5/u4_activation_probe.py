"""Cold pinned-source activation probe; ordinary import reads are permitted."""
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'skills/hanig-orchestrate/scripts'))
assert not any(n.startswith('d5_') for n in sys.modules)


def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR):
            raise AssertionError('activation write: ' + str(path))
        if not isinstance(path, str) or not path.endswith(('.py', '.pyc')):
            raise AssertionError('activation non-source read: ' + str(path))
    if event.startswith(('subprocess.', 'socket.')) or event in (
            'fcntl.flock', 'fcntl.lockf', 'os.mkdir', 'os.rename', 'os.remove',
            'os.rmdir', 'os.chmod', 'os.link', 'os.symlink', 'os.system'):
        raise AssertionError('activation effect: ' + event)


def profile(frame, event, arg):
    if event == 'call' and frame.f_globals.get('__name__') == 'd5_registry':
        if frame.f_code.co_name not in ('<module>', 'd5_active'):
            raise AssertionError('activation registry access: ' + frame.f_code.co_name)


sys.addaudithook(audit)
sys.setprofile(profile)
import d5_registry
assert d5_registry.d5_active() is False
sys.setprofile(None)
print('inactive; no storage/process/lock effects')
