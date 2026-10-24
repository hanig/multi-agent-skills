"""Opt-in merge journal obligation; U4 production routing remains unavailable.

Supported envelope and accepted limits
-------------------------------------
- Merge-journal recording only.
- No arbitrary callback framework.
- No journal IO under START.
- No new authorization check during Finish.
- No automatic retry of an uncertain append.

Trusted callers supply factual attestations. Prepare is immutable; bind belongs
to the submitting owner (a future R8b child binds locally). On append failure,
retain the journal and reconcile against the ledger without replay. No crash
exactly-once guarantee.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path

import d5_effect_log as ledger
import swarm


@dataclass(frozen=True)
class MergeJournal:
    path: str
    payload: bytes


def prepare(state_dir, record):
    """No IO; use the real recorder's target with an explicit absolute state."""
    if not Path(state_dir).is_absolute():
        raise ValueError("D5 merge journal requires an absolute state path")
    problem = swarm.SE._merge_shape_problem(record)
    if problem:
        raise ValueError(problem)
    return MergeJournal(str(swarm.merge_journal_path(state_dir)),
                        json.dumps(record, sort_keys=True, allow_nan=False).encode())


@dataclass(frozen=True)
class BoundJournal:
    specification: MergeJournal
    operation: object
    directory: str
    owner: int
    _state: dict = field(default_factory=dict, repr=False, compare=False)

    def _owned(self):
        if self.owner != os.getpid():
            raise ledger.EffectError("D5 journal belongs to another invocation owner")

    def attach(self, handle):
        self._owned()
        binding = json.loads(handle._binding)
        op = self.operation
        if (type(handle) is not ledger.Receipt or handle._directory != self.directory or
                binding["kind"] != op.kind or binding["scope"] != json.loads(op.scope) or
                binding["correlation"] != {"request_id": op.request_id, "intent": bytes.hex(op.intent)} or
                self._state.get("effect_id", handle.effect_id) != handle.effect_id):
            raise ledger.EffectError("D5 journal invocation mismatch")
        self._state["effect_id"] = handle.effect_id

    def record(self, outcome):
        self._owned()
        if "effect_id" not in self._state or self._state.get("attempted"):
            raise ledger.EffectError("D5 journal requires reconciliation; no replay")
        self._state["attempted"] = True
        if outcome["status"] == "succeeded":
            swarm.SE.load_merge_receipts(str(Path(self.specification.path).parent))
            swarm.SJ._fsync_append(Path(self.specification.path),
                                   json.loads(self.specification.payload))


def bind(specification, operation, state_dir, directory):
    """Invocation-specific binding before Start; no journal IO or ledger handle."""
    if (type(specification) is not MergeJournal or type(specification.payload) is not bytes or
            specification != prepare(state_dir, json.loads(specification.payload))):
        raise ledger.EffectError("D5 merge journal target mismatch")
    return BoundJournal(specification, operation, str(directory),
                        os.getpid())
