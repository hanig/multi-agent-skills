"""Real installation publication; no activation patch or hand-written records."""
from pathlib import Path
import uuid

import d5_install as install
import d5_readiness as readiness
import d5_protocol_locks as protocol
import swarm_state as state


def ready_installation(tmp, entry=None):
    """Publish under the real installer lease and DRAIN/START protocol.

    A standalone fixture needs only filesystem bindings, not grant authority.
    Registry consumers supply the entry returned by real installer staging.
    """
    tmp = Path(tmp).resolve()
    if entry is None:
        directory, record = tmp / 'state', tmp / 'record'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        record.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = lambda p: [p.stat().st_dev, p.stat().st_ino]
        entry = dict(registration_id=uuid.uuid4().hex, plan_id='p',
                     state_dir=str(directory), record_dir=str(record),
                     state_key=key(directory), record_key=key(record), repo_key=key(tmp))
    with readiness.installer_lease(entry['state_dir']):
        with protocol.pin(entry['record_dir']) as pinned:
            with pinned.drain(shared=False), pinned.start():
                readiness.successor_gate(entry)
                readiness.withdraw(Path(entry['state_dir']))
                readiness.publish_installation(entry)
    return entry


def ready(case, checkout, plan_id, state_dir, promote=True):
    ok, reason = state.acquire_lease(state_dir, bump_epoch=False)
    if not ok:
        raise AssertionError(reason)
    case.addCleanup(state.release_lease, state_dir)
    entry = install.stage(checkout, plan_id, state_dir)
    if promote:
        ready_installation(Path(state_dir).parent, entry)
    return entry
