"""Opt-in merge-journal completion."""
from dataclasses import dataclass, field
import json, os, threading
from pathlib import Path

import d5_effect_log as L
import d5_completion as completion
import d5_protocol_locks as protocol
import swarm

@dataclass(frozen=True)
class MergeJournal:
  path: str
  payload: bytes

def prepare(state_dir, record):
  if not Path(state_dir).is_absolute():
    raise ValueError("D5 journal path must be absolute")
  problem = swarm.SE._merge_shape_problem(record)
  if problem:
    raise ValueError(problem)
  return MergeJournal(str(swarm.merge_journal_path(state_dir)), json.dumps(record, sort_keys=True, allow_nan=False).encode())

@dataclass(frozen=True)
class BoundJournal:
  spec: MergeJournal
  operation: object
  directory: str
  owner: int
  args: tuple
  _state: dict = field(default_factory=dict, repr=False, compare=False)
  _lock: object = field(default_factory=threading.RLock, repr=False, compare=False)

  def _owned(self):
    if self.owner != os.getpid():
      raise L.EffectError("D5: another invocation owner")

  def attach(self, handle):
    self._owned()
    binding = json.loads(handle._binding)
    op = self.operation
    if (type(handle) is not L.Receipt or handle._directory != self.directory or binding["kind"] != op.kind or binding["scope"] != json.loads(op.scope) or
      binding["correlation"] != {"request_id": op.request_id, "intent": bytes.hex(op.intent)} or self._state.get("handle", handle) != handle):
      raise L.EffectError("D5 journal invocation mismatch")
    self._state["handle"] = handle

  def record(self, outcome):
    self._owned()
    if "handle" not in self._state or self._state.get("attempted"):
      raise L.EffectError("D5 journal uncertain; no replay")
    self._state["attempted"] = True
    if outcome["status"] == "succeeded":
      swarm.SE.load_merge_receipts(str(Path(self.spec.path).parent))
      swarm.SJ._fsync_append(Path(self.spec.path), json.loads(self.spec.payload))

  def complete(self, outcome):
    self._owned()
    handle = self._state["handle"]
    with completion._refusals(), protocol.pin(self.directory) as record, record.drain():
      if record._record._prior(1) is not None:
        raise L.EffectError("D5 journal inside START")
      with self._lock:
        L._active()
        L._outcome(outcome)
        with L._checked(handle) as (_, _, entry):
          L._require(type(handle) is L.Receipt and entry["state"] in ("submitted", "completed"), L.NOT_SUBMITTED)
          if entry["state"] == "completed":
            L._require(entry["outcome"] == outcome, L.CONFLICT)
        if self._state.get("completed") != L._encode(outcome):
          try:
            self.record(outcome)
          except BaseException:
            self.uncertain()
            raise
        result = L.complete(handle, outcome)
        self._state["completed"] = L._encode(result)
        return result

  def uncertain(self):
    self._owned()
    handle = self._state["handle"]
    with self._lock, completion._refusals(), protocol.pin(self.directory) as record, record.drain():
      L._require("completed" not in self._state, L.CONFLICT)
      with L._checked(handle) as (_, _, entry):
        completed = entry["state"] == "completed"
      if completed:
        # Reconcile the journal without altering the terminal effect.
        if "pending" not in self._state:
          b = json.loads(handle._binding)
          self._state["pending"] = L.prepare(*self.args,
            record_id=b["record_id"], custody=b["custody"], kind="merge_journal", scope=b["scope"], correlation=dict(request_id=handle.effect_id, journal=self.spec.path))
        attempt = self._state["pending"]
      else:
        attempt = handle
      return completion._Completion(attempt).uncertain()


def bind(spec, operation, args, directory):
  if (type(spec) is not MergeJournal or type(spec.payload) is not bytes or type(args) is not tuple or len(args) != 3 or spec != prepare(args[2], json.loads(spec.payload))):
    raise L.EffectError("D5 merge journal target mismatch")
  return BoundJournal(spec, operation, str(directory), os.getpid(), args)
