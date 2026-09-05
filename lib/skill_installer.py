#!/usr/bin/env python3
"""Planning primitives for the multi-agent skill installer.

This module deliberately keeps agent discovery and filesystem mutation at its
edges.  Discovery supplies adapter records; the lifecycle module performs the
actual copy/link.  Keeping the selection and planning decisions here makes it
possible to reject an unsafe request before either destination is touched.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# `install.sh` executes this file by path, which otherwise places only lib/ on
# sys.path.  Add the checkout root for the lifecycle package without changing
# the caller's working directory.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
if str(_CHECKOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(_CHECKOUT_ROOT))


SUPPORTED_AGENTS = ("claude", "codex", "opencode", "pi")
WORKFLOW_DEPENDENCIES = {"hanig-project": ("hanig-swarm",)}


class InstallRequestError(ValueError):
    """A command line request which must fail before writes."""


@dataclass(frozen=True)
class InstallOptions:
    agents: tuple[str, ...]
    excluded_agents: tuple[str, ...]
    prefix: Path | None
    mode: str
    only: tuple[str, ...]
    dry_run: bool
    force: bool
    allow_org_shadow: bool
    allow_vendored_shadow: bool
    include_vendored: bool
    uninstall: bool
    json: bool
    migrate_from: Path | None


@dataclass(frozen=True)
class AgentTarget:
    """One supported adapter, normalized from discovery's public records."""

    name: str
    state: str
    discovery_verified: bool
    automatic: bool
    destinations: tuple[Path, ...]
    consumers: tuple[str, ...] = ()

    @property
    def detected(self) -> bool:
        return self.state != "absent"


@dataclass(frozen=True)
class DestinationPlan:
    path: Path
    agents: tuple[str, ...]
    consumers: tuple[str, ...]
    logical_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class InstallPlan:
    selected: tuple[AgentTarget, ...]
    skipped: tuple[AgentTarget, ...]
    destinations: tuple[DestinationPlan, ...]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install.sh",
        description="Install skills for supported local coding agents.",
    )
    p.add_argument("--agent", action="append", default=[], metavar="NAME",
                   help="install for NAME; repeatable (claude, codex, opencode, pi)")
    p.add_argument("--exclude-agent", action="append", default=[], metavar="NAME",
                   help="skip a detected NAME; only valid in automatic mode")
    p.add_argument("--prefix", metavar="DIR",
                   help="legacy single arbitrary destination")
    p.add_argument("--mode", choices=("copy", "link"), default="copy")
    p.add_argument("--only", action="append", default=[], metavar="NAME",
                   help="install just NAME; repeatable")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--allow-org-shadow", action="store_true")
    p.add_argument("--allow-vendored-shadow", action="store_true")
    p.add_argument("--include-vendored", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--json", action="store_true", help="emit stable plan/result JSON")
    p.add_argument("--migrate-from", metavar="DIR",
                   help="show a read-only migration plan from DIR (requires --dry-run)")
    return p


def parse_options(argv: Sequence[str]) -> InstallOptions:
    ns = parser().parse_args(argv)
    agents = _names(ns.agent, "--agent")
    excluded = _names(ns.exclude_agent, "--exclude-agent")
    if ns.prefix and (agents or excluded):
        raise InstallRequestError(
            "--prefix is a single-destination compatibility mode and cannot "
            "be combined with --agent or --exclude-agent"
        )
    if agents and excluded:
        raise InstallRequestError(
            "--exclude-agent is only valid with automatic detection; use "
            "the --agent list to select an explicit subset"
        )
    if ns.migrate_from and not ns.dry_run:
        raise InstallRequestError("--migrate-from is read-only and requires --dry-run")
    return InstallOptions(
        agents=agents,
        excluded_agents=excluded,
        prefix=Path(ns.prefix).expanduser() if ns.prefix else None,
        mode=ns.mode,
        only=tuple(ns.only),
        dry_run=ns.dry_run,
        force=ns.force,
        allow_org_shadow=ns.allow_org_shadow,
        allow_vendored_shadow=ns.allow_vendored_shadow,
        include_vendored=ns.include_vendored,
        uninstall=ns.uninstall,
        json=ns.json,
        migrate_from=Path(ns.migrate_from).expanduser() if ns.migrate_from else None,
    )


def _names(names: Iterable[str], flag: str) -> tuple[str, ...]:
    result: list[str] = []
    for name in names:
        if name not in SUPPORTED_AGENTS:
            supported = ", ".join(SUPPORTED_AGENTS)
            raise InstallRequestError(
                f"unknown agent {name!r} for {flag}; supported agents: {supported}"
            )
        if name not in result:
            result.append(name)
    return tuple(result)


def normalize_agents(records: Iterable[Any]) -> tuple[AgentTarget, ...]:
    """Normalize discovery records without owning discovery policy.

    The discovery API exposes plain mappings today; accepting attributes as
    well keeps this consumer resilient if it graduates to dataclasses.
    """
    result: list[AgentTarget] = []
    for record in records:
        name = _field(record, "name", _field(record, "id", None))
        if name not in SUPPORTED_AGENTS:
            continue
        raw_paths = _field(record, "destinations", ())
        destinations = tuple(
            Path(_field(item, "path", item)).expanduser()
            for item in raw_paths
        )
        consumers = tuple(str(x) for x in _field(record, "consumers", ()))
        result.append(AgentTarget(
            name=name,
            state=str(_field(record, "state", "executable_found"
                            if _field(record, "detected", False) else "absent")),
            discovery_verified=_field(record, "verification", "unverified") == "verified",
            automatic=bool(_field(record, "eligible_for_automatic_target",
                                  _field(record, "detected", False))),
            destinations=destinations,
            consumers=consumers,
        ))
    return tuple(result)


def _field(record: Any, key: str, default: Any) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def select_agents(targets: Iterable[AgentTarget], options: InstallOptions) -> tuple[
        tuple[AgentTarget, ...], tuple[AgentTarget, ...]]:
    """Choose targets. Explicit choices are intentionally allowed unverified."""
    by_name = {target.name: target for target in targets}
    if options.agents:
        selected = tuple(by_name[name] for name in options.agents if name in by_name)
        missing = [name for name in options.agents if name not in by_name]
        if missing:
            raise InstallRequestError(
                "discovery does not provide adapter(s): " + ", ".join(missing)
            )
        return selected, tuple(target for target in targets if target not in selected)

    selected = tuple(
        target for target in targets
        if target.automatic and target.name not in options.excluded_agents
    )
    skipped = tuple(target for target in targets if target not in selected)
    if not selected:
        raise InstallRequestError(
            "no supported agents were detected; install an agent or choose an "
            "explicit target with --agent claude|codex|opencode|pi"
        )
    return selected, skipped


def build_plan(targets: Iterable[AgentTarget], options: InstallOptions) -> InstallPlan:
    selected, skipped = select_agents(targets, options)
    by_destination: dict[Path, list[AgentTarget]] = {}
    for target in selected:
        if not target.destinations:
            raise InstallRequestError(f"{target.name} has no skill destination")
        for destination in target.destinations:
            by_destination.setdefault(destination, []).append(target)
    destinations = tuple(
        DestinationPlan(
            path=path,
            agents=tuple(target.name for target in targets_at_path),
            consumers=tuple(
                consumer for target in targets_at_path for consumer in target.consumers
            ),
        )
        for path, targets_at_path in by_destination.items()
    )
    return InstallPlan(selected=selected, skipped=skipped, destinations=destinations)


def build_discovery_plan(report: Mapping[str, Any], selection: Mapping[str, Any]) -> InstallPlan:
    """Translate ARC-275's authoritative multi-target selection into our plan."""
    if not selection["selected"]:
        raise InstallRequestError(
            "no supported agents were detected; install an agent or choose an "
            "explicit target with --agent claude|codex|opencode|pi"
        )
    selected: list[AgentTarget] = []
    selected_by_path: dict[Path, list[str]] = {}
    for item in selection["selected"]:
        name = item["agent"]
        record = report["agents"][name]
        path = Path(item["destination"]["physical_path"])
        selected_by_path.setdefault(path, []).append(name)
        selected.append(AgentTarget(
            name=name, state=record["state"],
            discovery_verified=record["verification"] == "verified",
            automatic=record["eligible_for_automatic_target"], destinations=(path,),
            consumers=tuple(item["consumers"]),
        ))
    skipped = tuple(AgentTarget(
        name=item["agent"], state=report["agents"][item["agent"]]["state"],
        discovery_verified=report["agents"][item["agent"]]["verification"] == "verified",
        automatic=report["agents"][item["agent"]]["eligible_for_automatic_target"],
        destinations=(), consumers=(),
    ) for item in selection["skipped"])
    destinations = tuple(DestinationPlan(
        path=Path(item["physical_path"]),
        agents=tuple(item.get("selected_agents", selected_by_path[Path(item["physical_path"])])),
        consumers=tuple(item["consumers"]),
        logical_paths=(Path(item["destination"]["logical_path"]),),
    ) for item in selection["destinations"])
    return InstallPlan(tuple(selected), skipped, destinations)


def render_plan(plan: InstallPlan, options: InstallOptions, version: str) -> str:
    lines = [f"Install mode: {options.mode}  version: {version}", "Detected agents:"]
    for target in plan.selected + plan.skipped:
        lines.append(f"  {target.name}: {target.state}")
    lines.append("Selected agents:")
    for target in plan.selected:
        if target.name == "prefix":
            verified = "unverified (arbitrary compatibility destination)"
        elif target.discovery_verified:
            verified = "adapter version matches documented policy (not native loader verification)"
        else:
            verified = "unverified (explicit selection)"
        lines.append(f"  {target.name}: {verified}")
    if plan.skipped:
        lines.append("Skipped agents:")
        lines.extend(f"  {target.name}" for target in plan.skipped)
    lines.append("Destinations:")
    for destination in plan.destinations:
        agents = ", ".join(destination.agents)
        consumers = ", ".join(destination.consumers) or "default consumer"
        lines.append(f"  {destination.path} <- {agents} ({consumers})")
    lines.append("Workflow dependency limits: skill payloads are installed only; "
                 "external CLIs and workflow dependencies are not installed.")
    return "\n".join(lines)


def _load_discovery(repo: Path):
    path = repo / "skills" / "hanig-project" / "scripts" / "agent_discovery.py"
    spec = importlib.util.spec_from_file_location("hanig_agent_discovery", path)
    if spec is None or spec.loader is None:
        raise InstallRequestError(f"cannot load discovery contract: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def source_version(repo: Path) -> str:
    try:
        version = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            text=True, capture_output=True, check=True,
        ).stdout
        return version + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def selected_sources(repo: Path, only: Sequence[str]) -> list[tuple[str, Path, str]]:
    root = repo / "skills"
    if not root.is_dir():
        raise InstallRequestError(f"no skills directory at {root}")
    available = {path.name: path for path in root.iterdir()
                 if path.is_dir() and not path.name.startswith(".")
                 and ".bak" not in path.name}
    missing = [name for name in only if name not in available]
    if missing:
        raise InstallRequestError("unknown skill(s) for --only: " + ", ".join(missing))
    requested = set(only)
    missing_dependencies = [
        f"{name} requires {dependency}"
        for name, dependencies in WORKFLOW_DEPENDENCIES.items() if name in requested
        for dependency in dependencies if dependency not in requested
    ]
    if missing_dependencies:
        raise InstallRequestError(
            "--only omits workflow dependencies: " + ", ".join(missing_dependencies) +
            "; include the dependency explicitly"
        )
    names = list(only) if only else sorted(available)
    return [(name, available[name], "authored" if name.startswith("hanig-") else "vendored")
            for name in names]


def validate_payload(path: Path) -> None:
    """Validate without py_compile, so --dry-run cannot create bytecode caches."""
    skill = path / "SKILL.md"
    if not skill.is_file():
        raise ValueError("missing SKILL.md")
    try:
        text = skill.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ValueError(f"cannot read SKILL.md: {exc}") from exc
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md lacks YAML frontmatter")
    if not any(line.startswith("name:") for line in text.splitlines()):
        raise ValueError("SKILL.md has no name field")
    for script in path.glob("scripts/*.py"):
        try:
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ValueError(f"{script.name} fails to compile: {exc}") from exc
    for script in path.glob("scripts/*.sh"):
        completed = subprocess.run(["sh", "-n", str(script)], text=True,
                                   capture_output=True, check=False)
        if completed.returncode:
            raise ValueError(f"{script.name} has a syntax error")


def _legacy_prefix_plan(prefix: Path, options: InstallOptions) -> InstallPlan:
    target = AgentTarget("prefix", "compatibility destination", False, True,
                         (Path(os.path.abspath(prefix)),), ())
    return build_plan((target,), options)


def _org_shadow_conflicts(plan: InstallPlan, names: Iterable[str], env: Mapping[str, str]) -> list[str]:
    """Retain the documented Arc org-store shadow warning before mutations."""
    if not any("claude" in destination.consumers for destination in plan.destinations):
        return []
    names = tuple(names)
    home = Path(env["HOME"])
    orgs = home / ".claude-science" / "orgs"
    if not orgs.is_dir():
        return []
    conflicts = []
    for org in orgs.iterdir():
        store = org / "skills"
        for name in names:
            if (store / name).exists():
                conflicts.append(f"{name} also exists in org-managed store {store}")
    return conflicts


def _lifecycle_targets(plan: InstallPlan, sources: Sequence[tuple[str, Path, str]],
                       options: InstallOptions, version: str):
    from lib.skill_lifecycle import LifecycleTarget
    targets = []
    for destination in plan.destinations:
        for name, source, origin in sources:
            final_destination = destination.path / name
            if os.path.commonpath((str(source), str(final_destination))) == str(source):
                raise InstallRequestError(
                    f"refusing destination inside source payload: {final_destination}"
                )
            targets.append(LifecycleTarget(
                name=name, source=source, destination=final_destination,
                # consumers in discovery are every loader that *can* see a
                # root.  Provenance must instead register only this plan's
                # actual selected agents, or uninstalling one would retain
                # phantom consumers that were never installed for.
                origin=origin, consumers=destination.agents, mode=options.mode,
                source_version=version,
                allow_foreign_replace=((origin == "authored" and options.force) or
                                       (origin == "vendored" and options.allow_vendored_shadow)),
            ))
    return targets


def _action(target, status: str, detail: str, plan: InstallPlan) -> dict[str, Any]:
    root = target.destination.parent
    destination = next(item for item in plan.destinations if item.path == root)
    return {"agents": list(destination.agents), "root": str(root),
            "skill": getattr(target, "name", target.destination.name),
            "status": status, "detail": detail}


def _document(*, operation: str, dry_run: bool, plan: InstallPlan,
              actions: Sequence[dict[str, Any]], diagnostics: Sequence[str],
              conflicts: Sequence[str], mode: str, version: str) -> dict[str, Any]:
    targets = []
    for target in plan.selected:
        for destination in target.destinations:
            targets.append({"agent": target.name, "root": str(destination),
                            "status": target.state,
                            "verification": ("adapter-version-verified" if target.discovery_verified
                                             else "unverified"),
                            "consumers": list(target.consumers)})
    return {"schema_version": 1, "operation": operation, "dry_run": dry_run,
            "mode": mode, "version": version,
            "targets": targets, "actions": list(actions),
            "skipped_agents": [{"agent": target.name, "status": target.state}
                               for target in plan.skipped],
            "diagnostics": list(diagnostics), "conflicts": list(conflicts)}


def _print(document: Mapping[str, Any], options: InstallOptions, plan: InstallPlan,
           version: str) -> None:
    if options.json:
        print(json.dumps(document, sort_keys=True))
        return
    print(render_plan(plan, options, version))
    for action in document["actions"]:
        print("%s: %s/%s — %s" % (action["status"], action["root"],
                                   action["skill"], action["detail"]))
    for diagnostic in document["diagnostics"]:
        print("note: " + diagnostic, file=sys.stderr)
    for conflict in document["conflicts"]:
        print("conflict: " + conflict, file=sys.stderr)


def _migration_plan(plan: InstallPlan, source: Path, only: Sequence[str]):
    """Use lifecycle's read-only migration planner without implying import."""
    from lib.skill_lifecycle import plan_migration
    if len(plan.destinations) != 1:
        raise InstallRequestError("--migrate-from needs one selected destination; choose one --agent")
    destination = plan.destinations[0].path
    return plan_migration(
        legacy_roots_by_consumer={"explicit-source": source},
        destination_for=lambda _consumer, name, _source: destination / name,
        selected_names=only or None,
    )


def run(argv: Sequence[str], *, repo: Path | None = None,
        env: Mapping[str, str] | None = None) -> int:
    """Public CLI implementation.  All planning/preflight completes before writes."""
    options = parse_options(argv)
    repo = (repo or Path(__file__).resolve().parent.parent).resolve()
    context = dict(os.environ if env is None else env)
    context.setdefault("HOME", str(Path.home()))
    version = source_version(repo)
    discovery = _load_discovery(repo)
    report = discovery.discover(env=context)
    selection = (None if options.prefix else
                 discovery.select_targets(report, options.agents, options.excluded_agents))
    plan = (_legacy_prefix_plan(options.prefix, options) if options.prefix
            else build_discovery_plan(report, selection))

    if options.migrate_from:
        items = _migration_plan(plan, options.migrate_from, options.only)
        actions = [{"agents": list(plan.destinations[0].agents),
                    "root": str(item.destination.parent), "skill": item.name,
                    "status": "migration-" + item.status, "detail": item.detail}
                   for item in items]
        document = _document(operation="migration-plan", dry_run=True, plan=plan,
                             actions=actions, diagnostics=["migration is a plan only; source content remains unchanged"],
                             conflicts=[], mode=options.mode, version=version)
        _print(document, options, plan, version)
        return 0

    if options.uninstall:
        from lib.skill_lifecycle import REPOSITORY_ID, read_provenance, uninstall
        candidates = []
        for destination in plan.destinations:
            if destination.path.is_dir():
                candidates.extend(path for path in destination.path.iterdir()
                                  if not path.name.startswith(".") and
                                  (not options.only or path.name in options.only))
            elif options.only:
                candidates.extend(destination.path / name for name in options.only)
        selected_consumers = () if options.prefix else tuple(target.name for target in plan.selected)
        if options.dry_run:
            actions = []
            for candidate in candidates:
                record = read_provenance(candidate)
                if not record or record.repository != REPOSITORY_ID:
                    status, detail = "retained", "no matching recorded ownership"
                elif record.origin == "vendored" and not options.include_vendored:
                    status, detail = "retained", "vendored payload requires include_vendored"
                elif record.origin == "unknown":
                    status, detail = "retained", "legacy origin is unknown; refusing destructive removal"
                elif selected_consumers and set(record.consumers) - set(selected_consumers):
                    status, detail = "would-retain-shared", "other recorded consumers remain"
                else:
                    status, detail = "would-remove", "exact recorded owned destination"
                class Preview:
                    destination = candidate
                    name = candidate.name
                actions.append(_action(Preview, status, detail, plan))
            document = _document(operation="uninstall", dry_run=True, plan=plan,
                                 actions=actions,
                                 diagnostics=["dry run — no destination or source files changed"], conflicts=[],
                                 mode=options.mode, version=version)
            _print(document, options, plan, version)
            return 0
        results = uninstall(candidates, consumers=selected_consumers,
                            include_vendored=options.include_vendored)
        actions = [_action(result, result.status, result.detail, plan) for result in results]
        document = _document(operation="uninstall", dry_run=options.dry_run, plan=plan,
                             actions=actions, diagnostics=[], conflicts=[],
                             mode=options.mode, version=version)
        _print(document, options, plan, version)
        return 0 if all(item.status in ("removed", "retained", "retained-shared") for item in results) else 1

    sources = selected_sources(repo, options.only)
    from lib.skill_lifecycle import install, preflight
    targets = _lifecycle_targets(plan, sources, options, version)
    inspection = preflight(targets, validator=validate_payload)
    inspected = [_action(item.target, item.action, item.reason, plan) for item in inspection]
    blocked = []
    for item in inspection:
        if item.action != "blocked":
            continue
        detail = f"{item.target.name} at {item.target.destination}: {item.reason}"
        if item.target.destination.is_symlink():
            detail += f" (symlink target: {os.readlink(item.target.destination)})"
        blocked.append(detail)
    org_conflicts = _org_shadow_conflicts(plan, (name for name, _, _ in sources), context)
    if org_conflicts and not options.allow_org_shadow:
        blocked.extend(org_conflicts)
    shadow_notes = (["org-managed shadow explicitly allowed: " + item
                     for item in org_conflicts] if options.allow_org_shadow else [])
    takeovers = [
        f"taking over '{target.name}' at {target.destination}: vendored skill already exists there"
        for target in targets
        if target.origin == "vendored" and target.allow_foreign_replace and
        (target.destination.exists() or target.destination.is_symlink())
    ]
    if options.dry_run or blocked:
        document = _document(operation="install", dry_run=options.dry_run, plan=plan,
                             actions=inspected,
                             diagnostics=(["dry run — no destination or source files changed"] + shadow_notes + takeovers
                                          if options.dry_run else takeovers),
                             conflicts=blocked, mode=options.mode, version=version)
        _print(document, options, plan, version)
        return 1 if blocked else 0

    results = install(targets, validator=validate_payload)
    actions = [_action(item.target, item.status, item.detail, plan) for item in results]
    failed = [item for item in results if item.status not in ("installed", "upgraded")]
    diagnostics = shadow_notes + takeovers
    if not failed and not options.only:
        # A removed source must not remain live forever, but lifecycle's exact
        # ownership proof remains the only authority to delete it.
        from lib.skill_lifecycle import uninstall
        shipped = {name for name, _source, _origin in sources}
        stale = [child for destination in plan.destinations if destination.path.is_dir()
                 for child in destination.path.iterdir()
                 if not child.name.startswith(".") and child.name not in shipped]
        pruned = uninstall(stale, include_vendored=options.include_vendored)
        actions.extend(_action(item, item.status, item.detail, plan) for item in pruned)
        removed = sum(item.status == "removed" for item in pruned)
        if removed:
            diagnostics.append(f"pruned {removed} no-longer-shipped owned skill(s)")
    document = _document(operation="install", dry_run=False, plan=plan,
                         actions=actions, diagnostics=diagnostics,
                         conflicts=[item.detail for item in failed],
                         mode=options.mode, version=version)
    _print(document, options, plan, version)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except InstallRequestError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
