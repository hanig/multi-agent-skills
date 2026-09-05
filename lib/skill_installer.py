#!/usr/bin/env python3
"""Planning primitives for the multi-agent skill installer.

This module deliberately keeps agent discovery and filesystem mutation at its
edges.  Discovery supplies adapter records; the lifecycle module performs the
actual copy/link.  Keeping the selection and planning decisions here makes it
possible to reject an unsafe request before either destination is touched.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SUPPORTED_AGENTS = ("claude", "codex", "opencode", "pi")


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


@dataclass(frozen=True)
class AgentTarget:
    """One supported adapter, normalized from discovery's public records."""

    name: str
    detected: bool
    destinations: tuple[Path, ...]
    consumers: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.detected


@dataclass(frozen=True)
class DestinationPlan:
    path: Path
    agents: tuple[str, ...]
    consumers: tuple[str, ...]


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
    if ns.uninstall and (agents or excluded):
        raise InstallRequestError(
            "--uninstall is only supported with --prefix until per-agent "
            "ownership selection is available"
        )
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
            detected=bool(_field(record, "detected", False)),
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
        if target.detected and target.name not in options.excluded_agents
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


def render_plan(plan: InstallPlan, options: InstallOptions, version: str) -> str:
    lines = [f"Install mode: {options.mode}  version: {version}", "Detected agents:"]
    for target in plan.selected + plan.skipped:
        state = "detected" if target.detected else "not detected"
        lines.append(f"  {target.name}: {state}")
    lines.append("Selected agents:")
    for target in plan.selected:
        verified = "verified" if target.verified else "unverified (explicit selection)"
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
