"""Real isolated installation and live coordinator lease for D5 consumers."""
import d5_install as install
import swarm_state as state


def ready(case, checkout, plan_id, state_dir, promote=True):
    ok, reason = state.acquire_lease(state_dir, bump_epoch=False)
    if not ok:
        raise AssertionError(reason)
    case.addCleanup(state.release_lease, state_dir)
    entry = install.stage(checkout, plan_id, state_dir)
    if promote:
        install.promote(checkout, plan_id, state_dir)
    return entry
