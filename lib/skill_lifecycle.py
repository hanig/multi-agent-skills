"""Safe, per-destination lifecycle primitives for skill installers.

This module intentionally does not discover homes or loader precedence.  The
CLI owns those policy decisions and passes fully resolved ``LifecycleTarget``
objects in.  A call to :func:`install` stages every viable copy before it
changes a destination, but publishing is *per destination*: callers must show
partial results rather than describe a multi-root operation as atomic.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Callable, Iterable, Mapping, Optional, Sequence


MARKER = ".installed-by-multi-agent-skills"
SIDECAR_DIR = ".multi-agent-skills-provenance"
REPOSITORY_ID = "multi-agent-skills"
SCHEMA_VERSION = "2"
VALID_ORIGINS = frozenset(("authored", "vendored", "unknown"))
VALID_MODES = frozenset(("copy", "link"))


def _absolute(path: Path | str) -> Path:
    """Normalize a caller-resolved path without following its final symlink."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _clean_values(values: Iterable[str]) -> tuple[str, ...]:
    values = tuple(sorted(set(values)))
    if any(not value or any(c in value for c in "\n\r,=") for value in values):
        raise ValueError("consumer names must be non-empty and contain no newline, comma, or =")
    return values


@dataclass(frozen=True)
class Provenance:
    """Ownership data persisted with an installed payload or link sidecar."""

    repository: str
    origin: str
    source_version: str
    destination: str
    consumers: tuple[str, ...]
    mode: str
    installed_at: str
    schema: str = SCHEMA_VERSION
    legacy: bool = False

    def __post_init__(self) -> None:
        if not self.repository or any(c in self.repository for c in "\n\r="):
            raise ValueError("repository must be a non-empty single-line value")
        if self.origin not in VALID_ORIGINS:
            raise ValueError(f"unknown origin: {self.origin}")
        if self.mode not in VALID_MODES:
            raise ValueError(f"unknown install mode: {self.mode}")
        object.__setattr__(self, "consumers", _clean_values(self.consumers))


@dataclass(frozen=True)
class LifecycleTarget:
    """One caller-selected payload and its already-resolved destination.

    ``provenance_path`` is normally omitted.  Copy installs use the marker in
    the payload; links use a deterministic sidecar beside the destination.
    Supplying it is useful where a loader has a designated metadata directory.
    """

    name: str
    source: Path | str
    destination: Path | str
    origin: str
    consumers: tuple[str, ...] = ()
    mode: str = "copy"
    source_version: str = "unknown"
    repository: str = REPOSITORY_ID
    provenance_path: Path | str | None = None
    allow_foreign_replace: bool = False

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or self.name in (".", ".."):
            raise ValueError("target name must be a single path component")
        if self.origin not in VALID_ORIGINS - {"unknown"}:
            raise ValueError("new installs must declare authored or vendored origin")
        if self.mode not in VALID_MODES:
            raise ValueError("mode must be copy or link")
        object.__setattr__(self, "source", _absolute(self.source))
        object.__setattr__(self, "destination", _absolute(self.destination))
        object.__setattr__(self, "consumers", _clean_values(self.consumers))
        if self.provenance_path is not None:
            object.__setattr__(self, "provenance_path", _absolute(self.provenance_path))


@dataclass(frozen=True)
class PreflightItem:
    target: LifecycleTarget
    action: str                 # install, upgrade, blocked
    reason: str
    previous: Provenance | None = None
    fingerprint: tuple | None = None


@dataclass(frozen=True)
class InstallResult:
    target: LifecycleTarget
    status: str                 # installed, upgraded, blocked, failed, skipped
    detail: str
    predecessor_recoverable: bool = False


@dataclass(frozen=True)
class UninstallResult:
    destination: Path
    status: str                 # removed, retained-shared, retained, blocked
    detail: str


@dataclass(frozen=True)
class MigrationItem:
    legacy_consumer: str
    name: str
    source: Path
    destination: Path
    status: str                 # ready, already-present, invalid-source
    detail: str


def _sidecar_path(destination: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(str(destination))).hexdigest()[:24]
    return destination.parent / SIDECAR_DIR / f"{destination.name}-{digest}.provenance"


def provenance_path(destination: Path | str, explicit: Path | str | None = None) -> Path:
    """Return the link sidecar path (or the caller's resolved path)."""
    return _absolute(explicit) if explicit is not None else _sidecar_path(_absolute(destination))


def _parse_marker(path: Path) -> Provenance | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    data: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key not in data:
            data[key] = value
    repository = data.get("repo")
    if not repository:
        return None
    # Older key/value markers prove who wrote the marker, but do not reliably
    # describe authorship, mode, consumers, or a path.  Preserve that lack of
    # knowledge: particularly, never promote a legacy vendored/foreign copy to
    # authored merely because it happens to have a familiar name.
    legacy = data.get("schema") != SCHEMA_VERSION
    origin = data.get("origin") if not legacy else "unknown"
    if origin not in VALID_ORIGINS:
        origin = "unknown"
        legacy = True
    mode = data.get("mode", "copy")
    if mode not in VALID_MODES:
        mode = "copy"
        legacy = True
    try:
        return Provenance(
            repository=repository,
            origin=origin,
            source_version=data.get("source_version", data.get("version", "unknown")),
            destination=data.get("destination", ""),
            consumers=tuple(filter(None, data.get("consumers", "").split(","))),
            mode=mode,
            installed_at=data.get("installed_at", "unknown"),
            schema=data.get("schema", "1"),
            legacy=legacy,
        )
    except ValueError:
        return None


def _record_location(destination: Path, explicit: Path | None = None) -> tuple[Provenance | None, Path | None]:
    # A directory-local marker wins: it travels with an atomic copied payload.
    marker = destination / MARKER
    if destination.is_dir() and not destination.is_symlink():
        record = _parse_marker(marker)
        if record:
            return record, marker
    sidecar = provenance_path(destination, explicit)
    record = _parse_marker(sidecar)
    return record, sidecar if record else None


def read_provenance(destination: Path | str, *, provenance_file: Path | str | None = None) -> Provenance | None:
    """Read a marker conservatively; unrecognized content is never ownership."""
    record, _ = _record_location(_absolute(destination),
                                 _absolute(provenance_file) if provenance_file else None)
    return record


def _serialize(record: Provenance) -> str:
    fields = (
        ("schema", record.schema), ("repo", record.repository),
        ("origin", record.origin), ("source_version", record.source_version),
        # Keep version for the existing POSIX installer while it adopts this API.
        ("version", record.source_version), ("destination", record.destination),
        ("consumers", ",".join(record.consumers)), ("mode", record.mode),
        ("installed_at", record.installed_at),
    )
    return "".join(f"{key}={value}\n" for key, value in fields)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_provenance(destination: Path | str, record: Provenance,
                     *, provenance_file: Path | str | None = None) -> Path:
    """Persist a record, using an external sidecar for links and other files."""
    dest = _absolute(destination)
    explicit = _absolute(provenance_file) if provenance_file else None
    path = dest / MARKER if dest.is_dir() and not dest.is_symlink() else provenance_path(dest, explicit)
    _write_atomic(path, _serialize(record))
    return path


def _fingerprint(path: Path, explicit: Path | None = None) -> tuple | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    record, location = _record_location(path, explicit)
    marker_text = ""
    if location:
        try:
            marker_text = location.read_text(encoding="utf-8")
        except OSError:
            marker_text = "unreadable"
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_mode, marker_text)


def _owned_by(record: Provenance | None, target: LifecycleTarget) -> bool:
    return bool(record and record.repository == target.repository)


def _source_error(target: LifecycleTarget, validator: Callable[[Path], None]) -> str | None:
    if not target.source.is_dir():
        return f"source is unavailable: {target.source}"
    try:
        validator(target.source)
    except Exception as exc:  # validator errors are actionable preflight failures
        return f"source validation failed: {exc}"
    return None


def validate_skill_payload(path: Path) -> None:
    """Minimal loader-independent validation for the currently shipped format."""
    if not (path / "SKILL.md").is_file():
        raise ValueError("missing SKILL.md")


def preflight(targets: Sequence[LifecycleTarget], *,
              validator: Callable[[Path], None] = validate_skill_payload) -> list[PreflightItem]:
    """Inspect only.  It never creates a directory, marker, or staging file."""
    seen: set[Path] = set()
    result: list[PreflightItem] = []
    for target in targets:
        if target.destination in seen:
            result.append(PreflightItem(target, "blocked", "two selected targets resolve to one destination"))
            continue
        seen.add(target.destination)
        source_error = _source_error(target, validator)
        if source_error:
            result.append(PreflightItem(target, "blocked", source_error))
            continue
        record = read_provenance(target.destination, provenance_file=target.provenance_path)
        fingerprint = _fingerprint(target.destination, target.provenance_path)
        if fingerprint is None:
            result.append(PreflightItem(target, "install", "destination is absent", record, fingerprint))
        elif _owned_by(record, target):
            result.append(PreflightItem(target, "upgrade", "existing destination is recorded as owned", record, fingerprint))
        elif target.allow_foreign_replace:
            result.append(PreflightItem(target, "upgrade", "explicit foreign replacement requested", record, fingerprint))
        else:
            result.append(PreflightItem(target, "blocked", "destination exists without this repository's ownership record", record, fingerprint))
    return result


def _record_for(target: LifecycleTarget) -> Provenance:
    return Provenance(
        repository=target.repository, origin=target.origin,
        source_version=target.source_version, destination=str(target.destination),
        consumers=target.consumers, mode=target.mode,
        installed_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )


@contextmanager
def _destination_lock(destination: Path):
    """Advisory OS lock, released even if an installer is interrupted."""
    digest = hashlib.sha256(os.fsencode(str(destination))).hexdigest()[:20]
    lock_path = destination.parent / f".multi-agent-skills-{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage(item: PreflightItem, validator: Callable[[Path], None]) -> Path:
    target = item.target
    stage = target.destination.parent / f".{target.name}.stage-{uuid.uuid4().hex}"
    try:
        if target.mode == "copy":
            shutil.copytree(target.source, stage, symlinks=True)
            write_provenance(stage, _record_for(target))
            validator(stage)
        else:
            # Link targets validate the source now, and stage their sidecar in
            # the destination filesystem before the old link is moved aside.
            validator(target.source)
            _write_atomic(stage, _serialize(_record_for(target)))
    except BaseException:
        _cleanup_stage(stage)
        raise
    return stage


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _cleanup_stage(stage: Path) -> None:
    if stage.exists() or stage.is_symlink():
        _remove_path(stage)


def _publish(item: PreflightItem, stage: Path) -> InstallResult:
    target = item.target
    dest = target.destination
    # staging a link's metadata file uses ``stage`` as a normal file; create a
    # distinct link after the predecessor is recoverably moved aside.
    backup: Path | None = None
    moved_predecessor = False
    try:
        if dest.exists() or dest.is_symlink():
            backup = dest.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
            os.replace(dest, backup)
            moved_predecessor = True
        if target.mode == "copy":
            os.replace(stage, dest)
        else:
            os.symlink(target.source, dest, target_is_directory=True)
            sidecar = provenance_path(dest, target.provenance_path)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, sidecar)
        if backup:
            _remove_path(backup)
        return InstallResult(target, "upgraded" if item.action == "upgrade" else "installed",
                             "published after staging and validation")
    except Exception as exc:
        restored = False
        restore_problem = ""
        # A new link may have been created before its sidecar could publish.
        # It is not a usable owned installation, so remove it before restoring
        # the predecessor (or before returning failure for a fresh install).
        if dest.exists() or dest.is_symlink():
            try:
                _remove_path(dest)
            except OSError as remove_exc:
                restore_problem = f"; incomplete replacement remains at {dest}: {remove_exc}"
        if moved_predecessor and backup and (backup.exists() or backup.is_symlink()):
            try:
                os.replace(backup, dest)
                restored = True
            except Exception as restore_exc:
                restore_problem = f"; predecessor remains recoverable at {backup}: {restore_exc}"
        return InstallResult(target, "failed", f"publish failed: {exc}{restore_problem}",
                             predecessor_recoverable=bool(backup and (restored or backup.exists() or backup.is_symlink())))


def install(targets: Sequence[LifecycleTarget], *,
            validator: Callable[[Path], None] = validate_skill_payload) -> list[InstallResult]:
    """Stage all selected targets then publish them independently.

    This is deliberately not a cross-root transaction.  A caller receives one
    result per target; a later failure never invalidates a result already
    published at another root, and each replaced predecessor is restored or
    left at a named backup path.
    """
    inspection = preflight(targets, validator=validator)
    blocked = [item for item in inspection if item.action == "blocked"]
    if blocked:
        return [InstallResult(item.target, "blocked", item.reason) for item in inspection]
    stages: dict[LifecycleTarget, Path] = {}
    try:
        for item in inspection:
            stages[item.target] = _stage(item, validator)
    except Exception as exc:
        for stage in stages.values():
            _cleanup_stage(stage)
        return [InstallResult(item.target, "failed" if item.target not in stages else "skipped",
                              f"staging failed before publish: {exc}") for item in inspection]
    results: list[InstallResult] = []
    for item in inspection:
        target, stage = item.target, stages[item.target]
        try:
            with _destination_lock(target.destination):
                # Detect an uncoordinated modification between inspection and
                # publish.  Refusing is safer than deciding which writer wins.
                if _fingerprint(target.destination, target.provenance_path) != item.fingerprint:
                    results.append(InstallResult(target, "blocked", "destination changed after preflight; retry"))
                else:
                    results.append(_publish(item, stage))
        finally:
            _cleanup_stage(stage)
    return results


def uninstall(destinations: Iterable[Path | str], *, consumers: Iterable[str] = (),
              repository: str = REPOSITORY_ID, include_vendored: bool = False,
              provenance_files: Mapping[Path | str, Path | str] | None = None) -> list[UninstallResult]:
    """Remove only exact recorded ownership; retaining shared consumers is safe.

    ``consumers`` means "detach these loaders."  When other recorded consumers
    remain, the payload stays installed and its provenance is narrowed.  Loader
    precedence is outside this module, so callers should explain that another
    loader may still make a same-name payload visible.
    """
    wanted_consumers = _clean_values(consumers)
    sidecars = {_absolute(k): _absolute(v) for k, v in (provenance_files or {}).items()}
    results: list[UninstallResult] = []
    for raw_dest in destinations:
        dest = _absolute(raw_dest)
        explicit = sidecars.get(dest)
        with _destination_lock(dest):
            record, location = _record_location(dest, explicit)
            if not (dest.exists() or dest.is_symlink()):
                results.append(UninstallResult(dest, "retained", "destination is already absent"))
                continue
            if not record or record.repository != repository:
                results.append(UninstallResult(dest, "blocked", "no matching recorded ownership"))
                continue
            if record.destination != str(dest):
                results.append(UninstallResult(dest, "blocked", "recorded destination does not match this path"))
                continue
            if wanted_consumers:
                if not record.consumers:
                    results.append(UninstallResult(dest, "retained", "legacy/consumerless record cannot prove selective ownership"))
                    continue
                remaining = tuple(value for value in record.consumers if value not in wanted_consumers)
                if remaining:
                    updated = replace(record, consumers=remaining)
                    write_provenance(dest, updated, provenance_file=explicit)
                    results.append(UninstallResult(dest, "retained-shared",
                                                   f"retained for recorded consumers: {', '.join(remaining)}"))
                    continue
            if record.origin == "vendored" and not include_vendored:
                results.append(UninstallResult(dest, "retained", "vendored payload requires include_vendored"))
                continue
            if record.origin == "unknown":
                results.append(UninstallResult(dest, "retained", "legacy origin is unknown; refusing destructive removal"))
                continue
            _remove_path(dest)
            if location and location.exists() and location != dest / MARKER:
                location.unlink()
            results.append(UninstallResult(dest, "removed", "removed exact recorded owned destination"))
    return results


def legacy_roots(home: Path | str) -> dict[str, Path]:
    """Known user-level legacy roots; no root is inspected recursively."""
    base = _absolute(home)
    return {
        "claude": base / ".claude" / "skills",
        "codex": base / ".codex" / "skills",
        "codex-xdg": base / ".config" / "codex" / "skills",
    }


def plan_migration(*, legacy_roots_by_consumer: Mapping[str, Path | str],
                   destination_for: Callable[[str, str, Path], Path | str],
                   selected_names: Iterable[str] | None = None) -> list[MigrationItem]:
    """Return a dry-run-only migration plan; it never deletes legacy content.

    The caller chooses the new destination for each (consumer, name, source)
    tuple.  Existing same-name content is reported but never replaced by this
    planning function, and no unrelated child is considered.
    """
    selected = set(selected_names) if selected_names is not None else None
    plan: list[MigrationItem] = []
    for consumer, raw_root in legacy_roots_by_consumer.items():
        root = _absolute(raw_root)
        if not root.is_dir():
            continue
        for source in sorted(root.iterdir(), key=lambda p: p.name):
            if selected is not None and source.name not in selected:
                continue
            if not source.is_dir() or source.is_symlink():
                continue
            destination = _absolute(destination_for(consumer, source.name, source))
            if not (source / "SKILL.md").is_file():
                plan.append(MigrationItem(consumer, source.name, source, destination,
                                          "invalid-source", "legacy directory lacks SKILL.md"))
            elif destination.exists() or destination.is_symlink():
                plan.append(MigrationItem(consumer, source.name, source, destination,
                                          "already-present", "new destination already exists; legacy remains untouched"))
            else:
                plan.append(MigrationItem(consumer, source.name, source, destination,
                                          "ready", "copy and verify new destination; never delete legacy automatically"))
    return plan
