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


def _canonical_source(path: Path | str) -> Path:
    """Resolve every source alias because sources are read, never replaced."""
    return Path(os.path.realpath(_absolute(path)))


def _canonical_destination(path: Path | str) -> Path:
    """Resolve parent aliases without following or replacing a final link target."""
    absolute = _absolute(path)
    return Path(os.path.realpath(absolute.parent)) / absolute.name


def _contains(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((str(parent), str(child))) == str(parent)
    except ValueError:
        return False


def _destinations_match(left: Path | str, right: Path | str) -> bool:
    return _canonical_destination(left) == _canonical_destination(right)


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
    link_target: str = ""
    link_identity: str = ""
    schema: str = SCHEMA_VERSION
    legacy: bool = False

    def __post_init__(self) -> None:
        if not self.repository or any(c in self.repository for c in "\n\r="):
            raise ValueError("repository must be a non-empty single-line value")
        if any("\n" in value or "\r" in value
               for value in (self.source_version, self.destination, self.installed_at, self.link_target, self.link_identity)):
            raise ValueError("provenance fields must be single-line values")
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
        if (not self.name or "\x00" in self.name or self.name.startswith(".") or
                "/" in self.name or "\\" in self.name):
            raise ValueError("target name must be a single path component")
        if self.origin not in VALID_ORIGINS - {"unknown"}:
            raise ValueError("new installs must declare authored or vendored origin")
        if self.mode not in VALID_MODES:
            raise ValueError("mode must be copy or link")
        if not self.source_version or "\n" in self.source_version or "\r" in self.source_version:
            raise ValueError("source_version must be a non-empty single-line value")
        source = _canonical_source(self.source)
        destination = _canonical_destination(self.destination)
        if _contains(source, destination) or _contains(destination, source):
            raise ValueError(
                f"source and destination payloads overlap: {source} and {destination}"
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "destination", destination)
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
    previous_owned: bool = False


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
    return (_canonical_destination(explicit) if explicit is not None else
            _sidecar_path(_canonical_destination(destination)))


def _parse_marker(path: Path) -> Provenance | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    data: dict[str, str] = {}
    malformed = False
    for line in lines:
        if not line:
            continue
        if "=" not in line:
            malformed = True
            continue
        key, value = line.split("=", 1)
        if not key or key in data:
            malformed = True
            continue
        data[key] = value
    repository = data.get("repo")
    if not repository:
        return None
    # Older key/value markers prove who wrote the marker, but do not reliably
    # describe authorship, mode, consumers, or a path.  Preserve that lack of
    # knowledge: particularly, never promote a legacy vendored/foreign copy to
    # authored merely because it happens to have a familiar name.
    legacy = data.get("schema") != SCHEMA_VERSION
    if not legacy:
        required = {
            "schema", "repo", "origin", "source_version", "version",
            "destination", "consumers", "mode", "installed_at",
            "link_target", "link_identity",
        }
        origin = data.get("origin")
        mode = data.get("mode")
        raw_consumers = data.get("consumers", "")
        consumers = (() if raw_consumers == "" else
                     tuple(raw_consumers.split(",")))
        invalid_link = (
            mode == "copy" and bool(data.get("link_target") or
                                    data.get("link_identity"))
        ) or (
            mode == "link" and
            (not data.get("link_target") or not data.get("link_identity") or
             not os.path.isabs(data["link_target"]))
        )
        if (malformed or not required.issubset(data) or
                origin not in VALID_ORIGINS - {"unknown"} or
                mode not in VALID_MODES or
                not data.get("source_version") or
                data.get("version") != data.get("source_version") or
                not data.get("destination") or
                not os.path.isabs(data["destination"]) or
                not data.get("installed_at") or
                (raw_consumers and any(not value for value in consumers)) or
                invalid_link):
            return None
    else:
        origin = "unknown"
        mode = data.get("mode", "copy")
        if mode not in VALID_MODES:
            mode = "copy"
        consumers = tuple(filter(None, data.get("consumers", "").split(",")))
    try:
        return Provenance(
            repository=repository,
            origin=origin,
            source_version=data.get("source_version", data.get("version", "unknown")),
            destination=data.get("destination", ""),
            consumers=consumers,
            mode=mode,
            installed_at=data.get("installed_at", "unknown"),
            link_target=data.get("link_target", ""),
            link_identity=data.get("link_identity", ""),
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
    record, _ = _record_location(
        _canonical_destination(destination),
        _canonical_destination(provenance_file) if provenance_file else None,
    )
    return record


def _serialize(record: Provenance) -> str:
    fields = (
        ("schema", record.schema), ("repo", record.repository),
        ("origin", record.origin), ("source_version", record.source_version),
        # Keep version for the existing POSIX installer while it adopts this API.
        ("version", record.source_version), ("destination", record.destination),
        ("consumers", ",".join(record.consumers)), ("mode", record.mode),
        ("installed_at", record.installed_at), ("link_target", record.link_target),
        ("link_identity", record.link_identity),
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
    dest = _canonical_destination(destination)
    explicit = _canonical_destination(provenance_file) if provenance_file else None
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


def _link_identity(destination: Path) -> str:
    """Stable attributes of the link itself, not of what it currently loads."""
    stat = destination.lstat()
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}"


def _record_matches_link(destination: Path, record: Provenance) -> bool:
    if not (destination.is_symlink() and record.link_target and record.link_identity):
        return False
    try:
        return (os.path.realpath(destination) == os.path.realpath(record.link_target)
                and _link_identity(destination) == record.link_identity)
    except OSError:
        return False


def _owned_by(record: Provenance | None, target: LifecycleTarget) -> bool:
    if not record or record.repository != target.repository:
        return False
    if record.destination and not _destinations_match(record.destination, target.destination):
        return False
    if record.mode != "link":
        return True
    # A sidecar alone is not ownership of a symlink.  It may be stale after a
    # user replaces the link, so verify the object is still a link to the
    # recorded target before allowing it to be upgraded or removed.
    return _record_matches_link(target.destination, record)


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
        sidecar = provenance_path(target.destination, target.provenance_path)
        previous_owned = _owned_by(record, target)
        if target.mode == "link" and fingerprint is None and sidecar.exists():
            if not previous_owned:
                result.append(PreflightItem(
                    target, "blocked",
                    "link provenance sidecar exists without this repository's ownership record",
                    record, fingerprint, previous_owned,
                ))
                continue
        if fingerprint is None:
            result.append(PreflightItem(
                target, "install", "destination is absent", record, fingerprint,
                previous_owned,
            ))
        elif previous_owned:
            result.append(PreflightItem(
                target, "upgrade", "existing destination is recorded as owned",
                record, fingerprint, previous_owned,
            ))
        elif target.allow_foreign_replace:
            result.append(PreflightItem(
                target, "upgrade", "explicit foreign replacement requested",
                record, fingerprint, previous_owned,
            ))
        else:
            result.append(PreflightItem(
                target, "blocked",
                "destination exists without this repository's ownership record",
                record, fingerprint, previous_owned,
            ))
    return result


def _record_for(target: LifecycleTarget, previous: Provenance | None = None,
                previous_owned: bool = False) -> Provenance:
    consumers = target.consumers
    if previous and previous_owned:
        consumers = _clean_values((*previous.consumers, *consumers))
    return Provenance(
        repository=target.repository, origin=target.origin,
        source_version=target.source_version, destination=str(target.destination),
        consumers=consumers, mode=target.mode,
        installed_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        link_target=str(target.source) if target.mode == "link" else "",
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
    token = f".{target.name}.stage-{uuid.uuid4().hex}"
    stage = (target.destination.parent / token / target.name
             if target.mode == "copy" else target.destination.parent / token)
    try:
        if target.mode == "copy":
            stage.parent.mkdir(parents=True, exist_ok=False)
            shutil.copytree(target.source, stage, symlinks=True)
            write_provenance(
                stage,
                _record_for(target, item.previous, item.previous_owned),
            )
            validator(stage)
        else:
            # Link targets validate the source now, and stage their sidecar in
            # the destination filesystem before the old link is moved aside.
            stage.parent.mkdir(parents=True, exist_ok=True)
            validator(target.source)
            _write_atomic(
                stage,
                _serialize(_record_for(
                    target, item.previous, item.previous_owned,
                )),
            )
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
    container = stage.parent
    if (container.name.startswith(f".{stage.name}.stage-") and
            len(container.name) == len(stage.name) + len("..stage-") + 32):
        try:
            container.rmdir()
        except FileNotFoundError:
            pass


def _publish(item: PreflightItem, stage: Path) -> InstallResult:
    target = item.target
    dest = target.destination
    # staging a link's metadata file uses ``stage`` as a normal file; create a
    # distinct link after the predecessor is recoverably moved aside.
    backup: Path | None = None
    sidecar_backup: Path | None = None
    sidecar: Path | None = None
    moved_predecessor = False
    moved_sidecar = False
    replacement_created = False
    published_sidecar = False
    try:
        predecessor_was_owned_link = bool(
            item.previous_owned and item.previous and item.previous.mode == "link"
        )
        if target.mode == "link" or predecessor_was_owned_link:
            sidecar = provenance_path(dest, target.provenance_path)
            # The old sidecar is the only durable proof for the old link.  A
            # failure after publishing a new sidecar must be able to restore
            # that proof as well as the link it describes.
            if sidecar.exists() or sidecar.is_symlink():
                sidecar_backup = (sidecar.parent /
                                  f".{target.name}.sidecar-backup-{uuid.uuid4().hex}")
                os.replace(sidecar, sidecar_backup)
                moved_sidecar = True
        if dest.exists() or dest.is_symlink():
            backup = dest.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
            os.replace(dest, backup)
            moved_predecessor = True
        if target.mode == "copy":
            os.replace(stage, dest)
            replacement_created = True
        else:
            os.symlink(target.source, dest, target_is_directory=True)
            replacement_created = True
            assert sidecar is not None
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, sidecar)
            published_sidecar = True
            # The staged record knew the intended target.  Bind its sidecar to
            # the newly-created link object before it can authorize ownership.
            write_provenance(dest, replace(_record_for(
                                                target, item.previous,
                                                item.previous_owned,
                                            ),
                                            link_identity=_link_identity(dest)),
                             provenance_file=target.provenance_path)
        if sidecar_backup:
            try:
                _remove_path(sidecar_backup)
            except OSError as exc:
                return InstallResult(target, "upgraded" if item.action == "upgrade" else "installed",
                                     f"published; predecessor sidecar cleanup failed and was retained at {sidecar_backup}: {exc}",
                                     predecessor_recoverable=True)
        if backup:
            try:
                _remove_path(backup)
            except OSError as exc:
                return InstallResult(target, "upgraded" if item.action == "upgrade" else "installed",
                                     f"published; predecessor cleanup failed and was retained at {backup}: {exc}",
                                     predecessor_recoverable=True)
        return InstallResult(target, "upgraded" if item.action == "upgrade" else "installed",
                             "published after staging and validation")
    except Exception as exc:
        restored = False
        restore_problem = ""
        # A new link may have been created before its sidecar could publish.
        # It is not a usable owned installation, so remove it before restoring
        # the predecessor (or before returning failure for a fresh install).
        if replacement_created and (dest.exists() or dest.is_symlink()):
            try:
                _remove_path(dest)
            except OSError as remove_exc:
                restore_problem = f"; incomplete replacement remains at {dest}: {remove_exc}"
        # A freshly staged sidecar must not survive a failed link publish: it
        # would describe a link that is gone (or has been restored to a prior
        # identity).  Put the predecessor's sidecar back before rebinding it.
        if published_sidecar and sidecar and (sidecar.exists() or sidecar.is_symlink()):
            try:
                _remove_path(sidecar)
            except OSError as remove_exc:
                restore_problem += f"; incomplete sidecar remains at {sidecar}: {remove_exc}"
        if moved_predecessor and backup and (backup.exists() or backup.is_symlink()):
            try:
                os.replace(backup, dest)
                restored = True
            except Exception as restore_exc:
                restore_problem = f"; predecessor remains recoverable at {backup}: {restore_exc}"
        if moved_sidecar and sidecar and sidecar_backup and (sidecar_backup.exists() or sidecar_backup.is_symlink()):
            if not (sidecar.exists() or sidecar.is_symlink()):
                try:
                    os.replace(sidecar_backup, sidecar)
                except Exception as restore_exc:
                    restore_problem += f"; predecessor sidecar remains recoverable at {sidecar_backup}: {restore_exc}"
        # Moving a link away and back changes its identity.  Rebind the
        # restored sidecar so the old, known-owned link is immediately
        # upgradeable/uninstallable rather than stranded by a failed upgrade.
        if (restored and item.previous_owned and item.previous and
                item.previous.mode == "link"):
            try:
                write_provenance(
                    dest, replace(item.previous, link_identity=_link_identity(dest)),
                    provenance_file=target.provenance_path,
                )
            except Exception as restore_exc:
                restore_problem += f"; restored link provenance could not be rebound: {restore_exc}"
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
    sidecars = {_canonical_destination(k): _canonical_destination(v)
                for k, v in (provenance_files or {}).items()}
    results: list[UninstallResult] = []
    for raw_dest in destinations:
        dest = _canonical_destination(raw_dest)
        explicit = sidecars.get(dest)
        with _destination_lock(dest):
            record, location = _record_location(dest, explicit)
            if not (dest.exists() or dest.is_symlink()):
                results.append(UninstallResult(dest, "retained", "destination is already absent"))
                continue
            if not record or record.repository != repository:
                results.append(UninstallResult(dest, "blocked", "no matching recorded ownership"))
                continue
            if not record.destination or not _destinations_match(record.destination, dest):
                results.append(UninstallResult(dest, "blocked", "recorded destination does not match this path"))
                continue
            if record.mode == "link" and not _record_matches_link(dest, record):
                results.append(UninstallResult(dest, "blocked", "link no longer matches its recorded target"))
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
