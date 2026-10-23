"""Fresh exec probe of R7 with the production activation default unpatched."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import test_d5_submission as t
import d5_effect_child as child


def inactive(directory, fd):
    assert t.registry.d5_active() is False
    boundary = t.DeterministicBoundary()
    before = os.get_inheritable(fd)
    with mock.patch.object(t.protocol, 'pin', wraps=t.protocol.pin) as pin, \
            mock.patch.object(os, 'dup', wraps=os.dup) as duplicate:
        try:
            child.bootstrap(fd, directory, 'fixture', 'p', 'fixture',
                grant_id='a' * 32, session_id='native', anchor=t.ANCHOR, generation=1,
                operation=t.submission.Operation('inactive', 'fixture', b'{}', b'local'),
                boundary=boundary)
        except t.authority.AuthorityError as exc:
            error = str(exc)
        else:
            error = None
    print(json.dumps(dict(error=error, pins=pin.call_count,
        duplicates=duplicate.call_count, before=before, after=os.get_inheritable(fd),
        retained=os.fstat(fd).st_ino > 0, calls=boundary.calls)), flush=True)
    # R7 retains its original on refusal; process exit releases protection.


if __name__ == '__main__':
    inactive(sys.argv[1], int(sys.argv[2]))
