"""Inert U3-R3 installation wrappers; no production callers or activation change.

Supported envelope and accepted limits
-------------------------------------
P/H; G for initial installation. A stuck shared holder can block indefinitely.
The caller supplies an already safely resolved, provisioned record directory D
and, for claim, its trusted current mandate locator. Directory locators containing
a .. component refuse before filesystem/protocol work; mandate locators retain ..
and are read under the locks. Other components stay intact.
P requires stable D and
lock inodes, same-UID ownership, 0700, and trusted ancestry. R1's opener repairs
owner permissions in place to 0600; it does not replace locks or chmod D.
Resource/open failures are permitted. Concurrent direct store mutation is
outside the participating envelope. Host evidence uses the measured d5_host
session anchor. Contexts remain lexical and thread-owned as specified by R1.

Non-claims: no forced takeover; no lease-store compatibility promise; no claim
about raw store claim or replace. This is not effect admission or provisioning.
Records are prospective grants, with consent supplied by the caller; the store
retains schema, replacement, custody and durability decisions. This module
does not turn record installation into permission to submit an effect.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import time

import d5_authority_store as store
import d5_host as host
import d5_protocol_locks as protocol
import d5_registry as registry
import d5_readiness as readiness


def _active(directory):
    installed = readiness.installation(directory)
    if not registry.d5_active(installed["state_dir"]):
        raise store.AuthorityError("D5 is inactive")


def _anchor():
    anchor = host.session_anchor()
    if isinstance(anchor, host.Unknown):
        raise store.AuthorityError("D5 anchor is not verifiable")
    return anchor


def _bound(path, cwd):
    """Bind a locator to entry cwd without normalizing filesystem components."""
    # The store consumes text locators; fsdecode preserves filesystem bytes.
    return os.path.join(os.fsdecode(cwd), os.fsdecode(path))


@contextmanager
def _installation(directory, mandate_path=None):
    paths = [os.fspath(directory)]
    if mandate_path is not None:
        paths.append(os.fspath(mandate_path))
    if ".." in os.fsdecode(paths[0]).split(os.sep):
        raise store.AuthorityError("D5 locator contains ..")
    cwd = os.getcwdb() if any(not os.path.isabs(p) for p in paths) else b""
    directory = _bound(paths[0], cwd)
    mandate_path = _bound(paths[1], cwd) if len(paths) == 2 else None
    _active(directory)
    with protocol.pin(directory) as record:
        with record.drain(shared=False), record.start():
            _active(directory)
            yield directory, _anchor(), mandate_path


def install_initial(directory, record):
    """Install in provisioned D; existing-record refusal belongs to create."""
    with _installation(directory) as (directory, _, _):
        return store.create(directory, record)


def replace_authority(directory, record):
    """Apply the store's replacement rules to the freshly read predecessor."""
    directory = os.fsdecode(directory)
    if ".." in directory.split(os.sep):
        raise store.AuthorityError("D5 locator contains ..")
    directory = _bound(directory, os.getcwdb() if not os.path.isabs(directory) else b"")
    # Resolve the state binding by local IO before any record lock acquisition.
    installed = readiness.installation(directory)
    with readiness.installer_lease(installed["state_dir"]) as state_dir:
        # Reload under the lease; an absent marker permits installer recovery.
        installed = readiness.installation(directory)
        readiness._require(installed["state_dir"] == str(state_dir) and
                           Path(installed["record_dir"]).samefile(directory),
                           "replacement installation binding mismatch")
        for path, key in ((state_dir, "state_key"), (Path(directory), "record_key")):
            readiness._require(list(registry._key(path.stat())) == installed[key],
                               "replacement installation object mismatch")
        with _installation(directory) as (directory, _, _):
            readiness.successor_gate(installed)
            # A consent/schema refusal must preserve the existing readiness.
            # Reuse the store's policy under DRAIN/START; replace rechecks it
            # under STATE when committing. Raw store writers are excluded.
            candidate = store.decode(store.encode(record))
            predecessor = store.read(directory)
            if candidate["custody"] is None:
                candidate["custody"] = predecessor["custody"]
            store.replacement_allowed(predecessor, candidate)
            store._unused_id(Path(directory), candidate)
            readiness.withdraw(state_dir)
            result = store.replace(directory, record)
            readiness.publish_installation(installed)
            return result


def claim_custody(directory, *, mandate_path):
    """Claim with fresh native identity, mandate bytes, time and holder liveness."""
    with _installation(directory, mandate_path) as (directory, (session_id, anchor), mandate_path):
        mandate = store.read_bytes(mandate_path)
        now = int(time.time())
        return store.claim(directory, now, mandate, session_id, anchor, host.liveness)


def stage(checkout, plan_id, state_dir):
    """Explicit installer scaffolding, independent of activation."""
    return registry.stage(checkout, plan_id, state_dir)


def promote(checkout, plan_id, state_dir):
    """Explicit installer publication of the restricted route profile."""
    return readiness.promote(checkout, plan_id, state_dir)
