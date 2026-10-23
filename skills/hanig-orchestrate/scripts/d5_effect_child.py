"""Inert U3-R7 protected-child bootstrap; no production callers or CLI.

Supported envelope and accepted limits
-------------------------------------
S/P/H: a fresh managed process receives an already-held shared DRAIN description
for stable, trusted, same-UID record/lock identities. R1 validates identity, not
lock ownership; the launcher must supply shared protection. Its merged opener
may repair owner permissions in place to 0600. Resource/open failures are
permitted. Raw forks, arbitrary executables and uninstrumented effect-capable
descendants are outside this envelope. Named trusted R6 Boundary adapters obey
the explicit start/finish contract. Bootstrap launch supplies no authority.

The registered checkout stays at the path recorded at registration for the registration's lifetime. A renamed or moved registered checkout makes admission refuse, before any store or boundary call, with the missing registered mandate path in the error; the grant, custody and mandate bytes are unchanged. Operator recovery: restore the checkout to its registered absolute path; recovery remains subject to ordinary expiry and the other admission checks.

The test harness may patch activation inside a test child. There is no
production environment variable, command-line switch, or protocol field that
activates D5. R4 accounting requires active D5 (ARC-3636-OD1).
Non-claims: no wrapping of arbitrary executables; no claim that inherited DRAIN
itself establishes authority. No parent-held receipt or capability is transferred
or reconstructed: the child submits its own operation through R6 (ARC-2872-OD1).
"""
from contextlib import ExitStack
import os

from d5_authority import AuthorityError
import d5_locks as locks
import d5_protocol_locks as protocol
import d5_submission as submission


def bootstrap(inherited_fd, directory, checkout, plan_id, state_dir, *,
              grant_id, session_id, anchor, generation, operation, boundary):
    """Own the inherited original; import R1's duplicate and submit afresh.

    Call on entry to the fresh child, outside START/STATE. The original is
    non-inheritable before pin/import. After R6 returns a confirmed terminal
    result and lexical cleanup succeeds, close it without unlocking. Uncertainty
    or an exception retains it until process exit, including admission refusals:
    R6 exposes no release capability on its exceptional path. The caller must
    exit that process, not retry or close retained protection while it lives.
    """
    os.set_inheritable(inherited_fd, False)
    with ExitStack() as stack:
        pinned = stack.enter_context(protocol.pin(directory))
        try:
            stack.enter_context(pinned.drain(inherited_fd=inherited_fd))
        except protocol.LockError as exc:
            if str(exc) != locks._REFERENCE:
                raise
            raise AuthorityError("D5 inherited drain mismatch") from exc
        result = submission.submit(checkout, plan_id, state_dir,
            grant_id=grant_id, session_id=session_id, anchor=anchor,
            generation=generation, operation=operation, boundary=boundary)
    # Normal certain returns have passed R6's terminal release gate. An unknown
    # result has not; neither it nor an exception authorizes this owner's close.
    if result.initiation in (submission.Initiation.SUBMITTED,
                             submission.Initiation.NOT_SUBMITTED):
        os.close(inherited_fd)
    return result
