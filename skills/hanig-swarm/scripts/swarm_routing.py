"""Data-loaded agent routing; no coordinator or network dependency.

Python 3.8+, standard library only.
"""

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# What a `code` unit runs unless it says otherwise, and the reasoning effort
# each model gets, live in ../agents.json. Models are data: this file names
# none. The file ships inside the skill so a cluster install carries it.
# A missing or malformed file stops the coordinator at import rather than
# dispatching a guessed default; there is deliberately no fallback model.
AGENTS_FILE = _HERE.parent / "agents.json"


def _routing_token(value):
    """A non-empty string with no whitespace anywhere: an id paseo accepts."""
    return (isinstance(value, str) and bool(value)
            and not any(c.isspace() for c in value))


def _routing_model(value):
    """PROVIDER/MODEL[/...]: every slash-separated segment a routing token."""
    return (_routing_token(value) and len(value.split("/")) >= 2
            and all(value.split("/")))


def load_agent_routing(path=AGENTS_FILE):
    """Read and check agents.json. Raises SystemExit naming the defect.

    Every value is checked the same way and returned exactly as written:
    nothing is stripped or normalized, because these strings go to paseo
    verbatim and a padded id would dispatch as an ERRORED agent.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("swarm: cannot read agent routing %s: %s" % (path, exc))
    default = data.get("default") if isinstance(data, dict) else None
    table = data.get("thinking_by_model") if isinstance(data, dict) else None
    provider = default.get("provider") if isinstance(default, dict) else None
    thinking = default.get("thinking") if isinstance(default, dict) else None
    if not _routing_model(provider):
        raise SystemExit("swarm: %s default.provider must be a PROVIDER/MODEL "
                         "string without whitespace or empty segments" % path)
    if not _routing_token(thinking):
        raise SystemExit("swarm: %s default.thinking must be a non-empty "
                         "thinking id without whitespace" % path)
    # An empty table is refused, not read as "every model at the default":
    # a table emptied by a bad edit would silently downgrade measured efforts.
    if not (isinstance(table, dict) and table and all(
            _routing_model(k) and _routing_token(v) for k, v in table.items())):
        raise SystemExit("swarm: %s thinking_by_model must be a non-empty map "
                         "of PROVIDER/MODEL strings to thinking ids, neither "
                         "with whitespace" % path)
    return provider, thinking, dict(table)


# A unit overrides any of it with `provider`, `model` or `thinking`. Setting
# `thinking` to null or "" turns the flag off entirely for a provider that has
# no such option.
DEFAULT_AGENT_PROVIDER, DEFAULT_AGENT_THINKING, THINKING_BY_MODEL = (
    load_agent_routing())
# The shipped pin. apply_agent_resolution starts from it every time, so a
# resolution applied for one plan never outlives that plan in this process.
PINNED_AGENT_PROVIDER = DEFAULT_AGENT_PROVIDER
PINNED_THINKING_BY_MODEL = dict(THINKING_BY_MODEL)


def _model_family():
    """The review gate's network-free model_family module, or None.

    Loaded by file path from the sibling hanig-review-gate skill (or
    HANIG_REVIEW_GATE_DIR), never through review.py or committee.py, which
    are network-capable and must not enter this process. Absent means no
    resolution: the pins in agents.json apply.
    """
    import importlib.util
    root = os.environ.get("HANIG_REVIEW_GATE_DIR") or str(
        _HERE.parent.parent / "hanig-review-gate")
    path = Path(root) / "scripts" / "model_family.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("hanig_model_family", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_agent_resolution(plan_path):
    """Adopt this project's resolved code-agent default, if its snapshot is valid.

    Called once when a command loads a plan, so the snapshot is looked up
    from the plan's directory upward to its project. A unit's own provider,
    model or thinking still wins. The resolved model inherits the pinned
    model's thinking level unless agents.json names one for it.
    """
    global DEFAULT_AGENT_PROVIDER
    DEFAULT_AGENT_PROVIDER = PINNED_AGENT_PROVIDER
    # In place, so every reference to the table sees the reset.
    THINKING_BY_MODEL.clear()
    THINKING_BY_MODEL.update(PINNED_THINKING_BY_MODEL)
    try:
        family = _model_family()
        if family is None:
            return DEFAULT_AGENT_PROVIDER
        routing = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
        digests = family.config_digests(
            Path(family.__file__).resolve().parent.parent / "reviewers.json",
            AGENTS_FILE)
        provider, note = family.resolved_agent_default(
            routing.get("default") or {}, routing.get("thinking_by_model") or {},
            digests, start=Path(plan_path).resolve().parent)
    except Exception as exc:
        # Resolution never stops validate or run: anything wrong here leaves
        # the shipped pin in force.
        print("swarm: routing: snapshot not applied, using the pin: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return DEFAULT_AGENT_PROVIDER
    if provider and note and provider != PINNED_AGENT_PROVIDER:
        THINKING_BY_MODEL[provider] = family.agent_thinking(
            routing.get("default") or {}, routing.get("thinking_by_model") or {},
            provider)
        DEFAULT_AGENT_PROVIDER = provider
        print("swarm: routing: " + note, file=sys.stderr)
    return DEFAULT_AGENT_PROVIDER


def default_thinking_for(u):
    """The reasoning effort one unit gets when it declares none.

    Keyed on the model that will actually run, which is the provider string
    unless the unit names a `model` separately -- both spellings reach paseo
    the same way, so both have to resolve here or the mapping would apply to
    one plan and not its equivalent. An unrecognised model falls back rather
    than refusing: a new model on the roster should dispatch at a sane effort,
    and the fallback is the level the two strongest models use.
    """
    provider = str(u.get("provider") or DEFAULT_AGENT_PROVIDER)
    model = u.get("model")
    key = "%s/%s" % (provider.split("/", 1)[0], model) if model else provider
    return THINKING_BY_MODEL.get(key, DEFAULT_AGENT_THINKING)
