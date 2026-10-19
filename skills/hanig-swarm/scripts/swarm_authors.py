"""Coordinator-recorded potential contributors; metadata, never authority."""

import shlex

from swarm_types import PlanError


def declared_author_history(unit):
    """Validate plan metadata without normalizing its identity bytes."""
    if "author_history" not in unit:
        return []
    history = unit["author_history"]
    prefix = f"unit {unit.get('id', '?')!r}: author_history"
    if not isinstance(history, list):
        raise PlanError(prefix + " must be a JSON list")
    if unit.get("kind") != "code":
        raise PlanError(prefix + " is supported only for kind=code")
    for record in history:
        if not isinstance(record, dict) or set(record) != {"unit", "attempt", "identity"}:
            raise PlanError(prefix + " records require exactly unit, attempt, identity")
        for key in ("unit", "attempt"):
            if not isinstance(record[key], str) or not record[key]:
                raise PlanError(prefix + "." + key + " must be a non-empty string")
        identity = record["identity"]
        if identity is not None:
            if not isinstance(identity, str):
                raise PlanError(prefix + ".identity must be provider/model or null")
            provider, _, model = identity.partition("/")
            if not provider or not model:
                raise PlanError(prefix + ".identity must be provider/model or null")
    return history


def intent_identity(intent):
    """Resolve frozen routing exactly as the completion protocol does."""
    provider_spec = intent.get("provider")
    provider, embedded_model = "", ""
    if isinstance(provider_spec, str):
        provider, _, embedded_model = provider_spec.partition("/")
    model = intent.get("model") or embedded_model
    if provider and isinstance(model, str) and model:
        return provider + "/" + model
    return None


def author_instruction(intent):
    """Render frozen contributors without borrowing an unknown current identity."""
    current = intent_identity(intent)
    known = [current] if current is not None else []
    unknown = []
    instructions = []
    if current is None:
        unknown.append((intent.get("unit_id", "UNKNOWN"),
                        intent.get("attempt_id", "UNKNOWN")))
        instructions.append(
            "The coordinator-recorded provider/model for this unit is "
            "UNKNOWN. Report the missing identity to the coordinator before "
            "review; do not invent an author or omit --author.")
    for record in intent.get("author_history", []):
        identity = record["identity"]
        if identity is None:
            unknown.append((record["unit"], record["attempt"]))
        elif identity not in known:
            known.append(identity)
    if known:
        flags = " ".join("--author " + shlex.quote(identity) for identity in known)
        subject = ("this unit's own model" if current is not None and len(known) == 1
                   else "all known contributing models")
        instructions.append(
            f"Pass {flags} on every review.py or committee.py run so the gate "
            f"excludes {subject}.")
    if unknown:
        records = "; ".join(f"unit {unit!r}, attempt {attempt!r}"
                            for unit, attempt in unknown)
        instructions.append(
            f"The recorded lineage is incomplete: {records}. "
            "Report these unknown identities to the coordinator before review; "
            "do not guess.")
    return " ".join(instructions)


def collect_author_history(unit, unit_state=None, exclude_attempt=None):
    """Copy ordered history, then each recorded attempt's history and identity.

    State comes from the coordinator, never a worker record. Legacy intents
    contribute their own identity (possibly unknown), but have no history.
    Deduplication preserves the first occurrence of the complete record, so
    unknown provenance is retained even beside a known identity for an attempt.
    """
    history, seen = [], set()

    def append(record):
        key = (record["unit"], record["attempt"], record["identity"])
        if key not in seen:
            seen.add(key)
            history.append(dict(record))

    for record in unit.get("author_history", []):
        append(record)
    intents = (unit_state or {}).get("attempt_launch_intents") or {}
    for attempt in sorted(intents):
        if attempt == exclude_attempt:
            continue
        intent = intents[attempt]
        if intent.get("schema_version", 1) >= 6:
            for record in intent.get("author_history", []):
                append(record)
        append({"unit": unit["id"], "attempt": attempt,
                "identity": intent_identity(intent)})
    return history
