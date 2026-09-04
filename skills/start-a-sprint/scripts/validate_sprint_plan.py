#!/usr/bin/env python3
"""Validate a start-a-sprint JSON plan before launching any agents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class PlanError(ValueError):
    """A deterministic sprint-plan contract violation."""


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{field} must be a non-empty string")
    return value.strip()


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanError(f"{field} must be a list")
    return value


def normalized_scope(scope: str) -> str:
    scope = scope.strip().replace("\\", "/")
    while scope.endswith("/**") or scope.endswith("/*"):
        scope = scope.rsplit("/", 1)[0]
    return scope.rstrip("/") or "/"


def scopes_overlap(left: str, right: str) -> bool:
    left = normalized_scope(left)
    right = normalized_scope(right)
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def scope_contains(parent: str, child: str) -> bool:
    parent = normalized_scope(parent)
    child = normalized_scope(child)
    return parent == child or child.startswith(f"{parent}/")


def validate_plan(plan: Any) -> dict[str, int]:
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    require_text(plan.get("sprint"), "sprint")
    monitor = plan.get("monitor")
    if not isinstance(monitor, dict):
        raise PlanError("monitor must be an object")
    if monitor.get("mode") != "mac-local":
        raise PlanError("monitor.mode must be 'mac-local'")
    monitor_url = require_text(monitor.get("url"), "monitor.url")
    parsed_monitor_url = urlparse(monitor_url)
    if parsed_monitor_url.scheme != "http" or parsed_monitor_url.hostname not in {"127.0.0.1", "localhost"}:
        raise PlanError("monitor.url must be an http loopback URL")
    require_text(monitor.get("command"), "monitor.command")

    experiment = plan.get("worker_experiment")
    if experiment is not None:
        if not isinstance(experiment, dict):
            raise PlanError("worker_experiment must be an object")
        require_text(experiment.get("comparison_key"), "worker_experiment.comparison_key")
        require_text(experiment.get("hypothesis"), "worker_experiment.hypothesis")

    pods = require_list(plan.get("pods"), "pods")
    if not pods:
        raise PlanError("pods must contain at least one ticket pod")
    if len(pods) > 3:
        raise PlanError(f"wave has {len(pods)} pods; maximum is 3")

    tickets: set[str] = set()
    branches: dict[str, str] = {}
    worktrees: dict[str, str] = {}
    pod_scopes: list[tuple[str, list[str]]] = []
    dependency_refs: dict[str, list[tuple[str, str | None]]] = {}
    worker_total = 0

    def claim(registry: dict[str, str], value: Any, field: str, owner: str) -> None:
        item = require_text(value, field)
        if item in registry:
            raise PlanError(f"duplicate {field} {item!r}: {registry[item]} and {owner}")
        registry[item] = owner

    for index, raw_pod in enumerate(pods):
        where = f"pods[{index}]"
        if not isinstance(raw_pod, dict):
            raise PlanError(f"{where} must be an object")
        ticket = require_text(raw_pod.get("ticket"), f"{where}.ticket")
        if ticket in tickets:
            raise PlanError(f"duplicate ticket {ticket!r}")
        tickets.add(ticket)
        require_text(raw_pod.get("acceptance_contract"), f"{where}.acceptance_contract")
        claim(branches, raw_pod.get("integration_branch"), f"{where}.integration_branch", ticket)
        claim(worktrees, raw_pod.get("integration_worktree"), f"{where}.integration_worktree", ticket)

        scopes = require_list(raw_pod.get("production_write_scopes"), f"{where}.production_write_scopes")
        if not scopes:
            raise PlanError(f"{where}.production_write_scopes must not be empty")
        normalized = [normalized_scope(require_text(scope, f"{where}.production_write_scopes")) for scope in scopes]
        pod_scopes.append((ticket, normalized))

        dependencies = require_list(raw_pod.get("dependencies", []), f"{where}.dependencies")
        dependency_refs[ticket] = []
        for dep_index, dependency in enumerate(dependencies):
            dep_where = f"{where}.dependencies[{dep_index}]"
            if not isinstance(dependency, dict):
                raise PlanError(f"{dep_where} must be an object")
            dependency_ticket = require_text(dependency.get("ticket"), f"{dep_where}.ticket")
            if dependency_ticket == ticket:
                raise PlanError(f"{dep_where} cannot depend on its own ticket {ticket!r}")
            if dependency.get("cleared") is not True:
                raise PlanError(f"{dep_where} is not cleared")
            evidence = dependency.get("evidence")
            if evidence is not None:
                evidence = require_text(evidence, f"{dep_where}.evidence")
            dependency_refs[ticket].append((dependency_ticket, evidence))

        workers = require_list(raw_pod.get("workers"), f"{where}.workers")
        if not 2 <= len(workers) <= 4:
            raise PlanError(f"{ticket} has {len(workers)} workers; each pod requires 2-4")
        worker_total += len(workers)
        worker_scopes: list[tuple[str, list[str]]] = []
        for worker_index, worker in enumerate(workers):
            worker_where = f"{where}.workers[{worker_index}]"
            if not isinstance(worker, dict):
                raise PlanError(f"{worker_where} must be an object")
            require_text(worker.get("task"), f"{worker_where}.task")
            executor = worker.get("executor")
            if not isinstance(executor, dict):
                raise PlanError(f"{worker_where}.executor must be an object")
            executor_kind = require_text(executor.get("kind"), f"{worker_where}.executor.kind")
            if executor_kind not in {"native", "paseo"}:
                raise PlanError(f"{worker_where}.executor.kind must be 'native' or 'paseo'")
            require_text(executor.get("model"), f"{worker_where}.executor.model")
            if executor_kind == "paseo":
                require_text(executor.get("provider"), f"{worker_where}.executor.provider")
            owner = f"{ticket} worker {worker_index + 1}"
            claim(branches, worker.get("branch"), f"{worker_where}.branch", owner)
            claim(worktrees, worker.get("worktree"), f"{worker_where}.worktree", owner)
            raw_worker_scopes = require_list(worker.get("write_scopes"), f"{worker_where}.write_scopes")
            if not raw_worker_scopes:
                raise PlanError(f"{worker_where}.write_scopes must not be empty")
            normalized_worker_scopes = [
                normalized_scope(require_text(scope, f"{worker_where}.write_scopes"))
                for scope in raw_worker_scopes
            ]
            for worker_scope in normalized_worker_scopes:
                if not any(scope_contains(pod_scope, worker_scope) for pod_scope in normalized):
                    raise PlanError(
                        f"{worker_where} scope {worker_scope!r} is outside {ticket}'s production write scopes"
                    )
            worker_scopes.append((owner, normalized_worker_scopes))

        for left_index, (left_owner, left_scopes) in enumerate(worker_scopes):
            for right_owner, right_scopes in worker_scopes[left_index + 1 :]:
                for left_scope in left_scopes:
                    for right_scope in right_scopes:
                        if scopes_overlap(left_scope, right_scope):
                            raise PlanError(
                                f"worker write scopes overlap within {ticket}: {left_owner} ({left_scope}) "
                                f"and {right_owner} ({right_scope})"
                            )

    if worker_total > 12:
        raise PlanError(f"wave has {worker_total} workers; maximum is 12")

    for ticket, dependencies in dependency_refs.items():
        for dependency_ticket, evidence in dependencies:
            if dependency_ticket not in tickets and evidence is None:
                raise PlanError(
                    f"{ticket} has external dependency {dependency_ticket!r} without clearance evidence"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ticket: str, path: list[str]) -> None:
        if ticket in visiting:
            cycle_start = path.index(ticket)
            cycle = " -> ".join([*path[cycle_start:], ticket])
            raise PlanError(f"ticket dependency cycle: {cycle}")
        if ticket in visited:
            return
        visiting.add(ticket)
        for dependency_ticket, _evidence in dependency_refs[ticket]:
            if dependency_ticket in tickets:
                visit(dependency_ticket, [*path, ticket])
        visiting.remove(ticket)
        visited.add(ticket)

    for ticket in tickets:
        visit(ticket, [])

    for left_index, (left_ticket, left_scopes) in enumerate(pod_scopes):
        for right_ticket, right_scopes in pod_scopes[left_index + 1 :]:
            for left_scope in left_scopes:
                for right_scope in right_scopes:
                    if scopes_overlap(left_scope, right_scope):
                        raise PlanError(
                            f"production write scopes overlap between {left_ticket} ({left_scope}) "
                            f"and {right_ticket} ({right_scope})"
                        )

    return {"pods": len(pods), "workers": worker_total}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Path to a sprint-plan JSON file")
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_text())
        counts = validate_plan(plan)
    except (OSError, json.JSONDecodeError, PlanError) as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 1
    print(f"VALID: {counts['pods']} pods, {counts['workers']} workers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
