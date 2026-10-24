"""Opt-in merge journal: durable keys, pre-Start markers, no uncertain replay."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import os
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
  if problem or any(k.startswith("d5_") for k in record):
    raise ValueError(problem or "reserved D5 journal fields")
  return MergeJournal(str(swarm.merge_journal_path(state_dir)),
            json.dumps(record, sort_keys=True, allow_nan=False).encode())


def _append_locked(fh, record):
  # Reuses the legacy journal inode/flock.
  swarm.SJ._heal_jsonl_tail(fh)
  fh.seek(0, os.SEEK_END)
  fh.write((json.dumps(record, sort_keys=True) + "\n").encode())
  fh.flush()
  os.fsync(fh.fileno())


@dataclass(frozen=True)
class BoundJournal:
  spec: MergeJournal
  operation: object
  directory: str
  owner: int
  args: tuple
  handle: object = None

  def _owned(self):
    if self.owner != os.getpid():
      raise L.EffectError("D5: another invocation owner")

  def attach(self, handle):
    self._owned()
    L._active()
    binding = json.loads(handle._binding)
    op = self.operation
    if (handle._directory != self.directory or binding["kind"] != op.kind or
        binding["scope"] != json.loads(op.scope) or
        binding["correlation"] != {"request_id": op.request_id, "intent": bytes.hex(op.intent)} or
        (self.handle is not None and self.handle.effect_id != handle.effect_id)):
      raise L.EffectError("D5 journal invocation mismatch")
    marker = dict(path=self.spec.path, payload=self.spec.payload.decode(), state="pending")
    with L._checked(handle) as (fd, name, entry):
      if "journal" in entry:
        L._require(all(entry["journal"][k] == marker[k] for k in ("path", "payload")), L.CONFLICT)
        L._confirm(fd, name)
      else:
        entry["journal"] = marker
        L._write(fd, name, entry)
    return replace(self, handle=handle)

  @property
  def key(self):
    return json.loads(self.handle._binding)["registration_id"] + ":" + self.handle.effect_id

  @contextmanager
  def _drain(self):
    self._owned()
    L._active()
    with completion._refusals(), protocol.pin(self.directory) as record, record.drain():
      if record._record._prior(1) is not None:
        raise L.EffectError("D5 journal inside START")
      yield

  @contextmanager
  def _locked(self):
    path = Path(self.spec.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as fh:
      fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
      yield fh

  def _lookup(self):
    rows, _ = swarm.SE.load_merge_receipts(str(Path(self.spec.path).parent))
    matches = [r for r in rows if r.get("d5_effect_key") == self.key]
    L._require(len(matches) <= 1, L.CONFLICT)
    if not matches:
      return None
    row = dict(matches[0])
    row.pop("d5_effect_key")
    outcome = row.pop("d5_outcome")
    L._outcome(outcome)
    L._require(row == json.loads(self.spec.payload) and outcome["status"] == "succeeded", L.CONFLICT)
    return outcome

  def _finish(self, outcome):
    with L._checked(self.handle) as (fd, name, entry):
      if entry["state"] == "completed":
        L._require(entry["outcome"] == outcome, L.CONFLICT)
      entry.update(state="completed", outcome=outcome)
      entry["journal"]["state"] = "completed"
      L._write(fd, name, entry)
    return outcome

  def _uncertain(self):
    with L._checked(self.handle) as (fd, name, entry):
      entry["journal"]["state"] = "uncertain"
      if entry["state"] in ("attempting", "submitted"):
        entry["state"] = "unknown"
      L._write(fd, name, entry)

  def record(self, outcome, *, reconcile=False):
    with self._locked() as fh:
      found = self._lookup()
      if reconcile:
        L._require(found is not None, "D5 journal absent; reconciliation unresolved")
        outcome = found
      with L._checked(self.handle) as (fd, name, entry):
        marker = entry["journal"]
        if entry["state"] == "completed":
          L._require(entry["outcome"] == outcome, L.CONFLICT)
        L._require(type(self.handle) is L.Receipt, L.NOT_SUBMITTED)
        if reconcile:
          L._require(entry["state"] in ("submitted", "unknown", "completed"), L.NOT_SUBMITTED)
        else:
          L._require(marker["state"] != "uncertain", "D5 journal uncertain; no replay")
          L._require(entry["state"] in ("submitted", "completed"), L.NOT_SUBMITTED)
        if found is not None:
          L._require(found == outcome, L.CONFLICT)
        else:
          L._require(marker["state"] == "pending", "D5 journal absent; no replay")
          # Crash before acknowledgement requires reconciliation.
          marker["state"] = "uncertain"
          L._write(fd, name, entry)
      try:
        if found is None:
          _append_locked(fh, dict(json.loads(self.spec.payload), d5_effect_key=self.key, d5_outcome=outcome))
        else:
          # Confirm durability even after an acknowledgement/fsync failure.
          os.fsync(fh.fileno())
        return self._finish(outcome)
      except BaseException:
        self._uncertain()
        raise

  def complete(self, outcome):
    with self._drain():
      L._outcome(outcome)
      outcome = L._decode(L._encode(outcome))
      with L._checked(self.handle) as (_, _, entry):
        L._require(type(self.handle) is L.Receipt and entry["state"] in ("submitted", "completed"), L.NOT_SUBMITTED)
        if entry["state"] == "completed":
          L._require(entry["outcome"] == outcome, L.CONFLICT)
      if outcome["status"] == "failed":
        # A failed effect owes no merge record.
        L.complete(self.handle, outcome)
        return self._finish(outcome)
      return self.record(outcome)

  def uncertain(self):
    with self._drain(), self._locked():
      self._lookup()
      self._uncertain()

  def reconcile(self):
    """Confirm a recorded success, never append or re-initiate an effect."""
    with self._drain():
      return self.record(None, reconcile=True)



def bind(spec, operation, args, directory):
  if (type(spec) is not MergeJournal or type(spec.payload) is not bytes or
      type(args) is not tuple or len(args) != 3 or
      spec != prepare(args[2], json.loads(spec.payload))):
    raise L.EffectError("D5 merge journal target mismatch")
  return BoundJournal(spec, operation, str(directory), os.getpid(), args)
