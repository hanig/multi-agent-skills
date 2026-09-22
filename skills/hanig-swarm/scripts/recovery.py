#!/usr/bin/env python3
"""Audit-only recovery snapshots for disposable code worktrees.

The coordinator calls this module before removing a worktree.  A snapshot is
published only after the source, preserved copy, and a restored copy have the
same content digest.  Git metadata is deliberately excluded: the separately
trusted base identity says where the work began, while this module preserves
the tracked and untracked working-tree bytes that cleanup would destroy.

Recovery records have no completion state, produced head, receipt, or resume
operation.  They can restore bytes into a new empty directory and nothing in
the judging path imports this module.  Preserving work therefore cannot close,
verify, or resume an attempt.

Python 3.8+, standard library only.
"""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path


PURPOSE = "audit-only-worktree-recovery"
SCHEMA_VERSION = 1
_RECORD_KEYS = frozenset({
    "schema_version", "purpose", "unit_id", "attempt_id", "base",
    "source_path", "snapshot_path", "content_sha256", "validation",
    "created_at",
})


def _entry_digest(hasher, root, path):
    relative = path.relative_to(root)
    name = os.fsencode(str(relative))
    try:
        info = path.lstat()
    except OSError as exc:
        return f"cannot stat {path}: {exc}"
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        hasher.update(b"D\0" + name + b"\0" + str(mode).encode() + b"\0")
        try:
            children = sorted(path.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            return f"cannot list {path}: {exc}"
        for child in children:
            if path == root and child.name == ".git":
                continue
            problem = _entry_digest(hasher, root, child)
            if problem:
                return problem
        return None
    if stat.S_ISREG(info.st_mode):
        hasher.update(b"F\0" + name + b"\0" + str(mode).encode() + b"\0")
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
        except OSError as exc:
            return f"cannot read {path}: {exc}"
        hasher.update(b"\0")
        return None
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            return f"cannot read symlink {path}: {exc}"
        hasher.update(b"L\0" + name + b"\0" + os.fsencode(target) + b"\0")
        return None
    return (f"unsupported filesystem entry {path}: mode {oct(info.st_mode)}; "
            "refusing cleanup rather than omitting bytes")


def _is_expected_git_pointer(path, expected_git_dir):
    """True only for the exact linked-worktree pointer Git created."""
    if expected_git_dir is None:
        return False
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return False
        raw = path.read_bytes()
        if not raw.startswith(b"gitdir: ") or not raw.endswith(b"\n"):
            return False
        target = raw[len(b"gitdir: "):-1]
        if not target or b"\n" in target or b"\r" in target:
            return False
        target_path = Path(os.fsdecode(target))
        if not target_path.is_absolute():
            target_path = path.parent / target_path
        return target_path.resolve() == Path(expected_git_dir).resolve()
    except (OSError, UnicodeError, ValueError):
        return False


def tree_digest(root, expected_git_dir=None):
    """Return ``(sha256, error)`` for worktree-owned filesystem objects.

    The exact linked-worktree pointer is Git metadata and may be excluded only
    when its target matches the coordinator-recorded Git directory. A replaced
    top-level ``.git`` object is ordinary worktree content and is preserved.
    """
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return None, f"recovery source {root} is not a real directory"
    digest = hashlib.sha256()
    try:
        children = sorted(root.iterdir(), key=lambda item: os.fsencode(item.name))
    except OSError as exc:
        return None, f"cannot list recovery source {root}: {exc}"
    for child in children:
        if (child.name == ".git"
                and _is_expected_git_pointer(child, expected_git_dir)):
            continue
        problem = _entry_digest(digest, root, child)
        if problem:
            return None, problem
    return digest.hexdigest(), None


def _copy_contents(source, destination, expected_git_dir=None):
    destination.mkdir(mode=0o700)
    for child in source.iterdir():
        if (child.name == ".git"
                and _is_expected_git_pointer(child, expected_git_dir)):
            continue
        target = destination / child.name
        try:
            if child.is_symlink():
                os.symlink(os.readlink(child), target)
            elif child.is_dir():
                shutil.copytree(child, target, symlinks=True)
            elif child.is_file():
                shutil.copy2(child, target, follow_symlinks=False)
            else:
                return (f"unsupported filesystem entry {child}; refusing "
                        "cleanup rather than omitting bytes")
        except OSError as exc:
            return f"cannot preserve {child}: {exc}"
    return None


def _fsync_tree(root):
    """Flush regular snapshot files and directories before publication."""
    for current, directories, files in os.walk(str(root), topdown=False,
                                                followlinks=False):
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                continue
            try:
                with open(path, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError as exc:
                return f"cannot flush recovery file {path}: {exc}"
        for name in directories:
            path = Path(current) / name
            if path.is_symlink():
                continue
            try:
                descriptor = os.open(str(path), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                return f"cannot flush recovery directory {path}: {exc}"
    return None


def _record(unit_id, attempt_id, base_commit, base_tree, source, snapshot,
            content_digest):
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "unit_id": str(unit_id),
        "attempt_id": str(attempt_id),
        "base": {"commit": str(base_commit), "tree": str(base_tree)},
        "source_path": str(source),
        "snapshot_path": str(snapshot),
        "content_sha256": content_digest,
        "validation": {
            "result": "restore-checked",
            "restored_content_sha256": content_digest,
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def validate_snapshot(record, unit_id=None, attempt_id=None,
                      base_commit=None, base_tree=None):
    """Validate coordinator-state binding and snapshot bytes.

    The manifest beside the bytes is an audit copy and is intentionally not
    read here.  Cleanup consumes only its coordinator-state record and a fresh
    digest of the preserved bytes.
    """
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        return "recovery record has an unknown or incomplete shape"
    if (record.get("schema_version") != SCHEMA_VERSION
            or record.get("purpose") != PURPOSE):
        return "recovery record is not an audit-only snapshot"
    expected = (("unit_id", unit_id), ("attempt_id", attempt_id))
    for key, value in expected:
        if value is not None and record.get(key) != str(value):
            return f"recovery record {key} does not match this attempt"
    base = record.get("base")
    if not isinstance(base, dict) or set(base) != {"commit", "tree"}:
        return "recovery record has no exact base identity"
    if base_commit is not None and base.get("commit") != str(base_commit):
        return "recovery record base commit does not match coordinator state"
    if base_tree is not None and base.get("tree") != str(base_tree):
        return "recovery record base tree does not match coordinator state"
    validation = record.get("validation")
    if (not isinstance(validation, dict)
            or set(validation) != {"result", "restored_content_sha256"}
            or validation.get("result") != "restore-checked"):
        return "recovery snapshot has no completed restore check"
    content = Path(str(record.get("snapshot_path"))) / "content"
    observed, problem = tree_digest(content)
    if problem:
        return problem
    expected_digest = record.get("content_sha256")
    if (not isinstance(expected_digest, str) or len(expected_digest) != 64
            or observed != expected_digest
            or validation.get("restored_content_sha256") != expected_digest):
        return "recovery snapshot content digest does not match its record"
    return None


def preserve_worktree(source, recovery_root, unit_id, attempt_id,
                      base_commit, base_tree, expected_git_dir=None):
    """Publish a restore-checked snapshot, returning ``(record, error)``."""
    supplied_source = Path(source)
    if supplied_source.is_symlink():
        return None, (f"recovery source {supplied_source} is a symlink; "
                      "refusing cleanup")
    source = supplied_source.resolve()
    recovery_root = Path(recovery_root).resolve()
    before, problem = tree_digest(source, expected_git_dir)
    if problem:
        return None, problem
    binding = json.dumps(
        [str(unit_id), str(attempt_id), str(base_commit), str(base_tree), before],
        separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    name = hashlib.sha256(binding).hexdigest()
    final = recovery_root / name
    try:
        recovery_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create recovery root {recovery_root}: {exc}"
    record = _record(unit_id, attempt_id, base_commit, base_tree, source,
                     final, before)
    if final.exists():
        problem = validate_snapshot(
            record, unit_id, attempt_id, base_commit, base_tree)
        if problem:
            return None, problem
        after, problem = tree_digest(source, expected_git_dir)
        if problem or after != before:
            return None, problem or "worktree changed while recovery was checked"
        return record, None

    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=str(recovery_root)))
    try:
        content = temporary / "content"
        problem = _copy_contents(source, content, expected_git_dir)
        if problem:
            return None, problem
        copied, problem = tree_digest(content)
        if problem or copied != before:
            return None, problem or "preserved copy differs from its source"
        restored = temporary / "restore-check"
        problem = _copy_contents(content, restored)
        if problem:
            return None, problem
        restored_digest, problem = tree_digest(restored)
        if problem or restored_digest != before:
            return None, problem or "restored recovery copy differs from its source"
        shutil.rmtree(str(restored))
        after, problem = tree_digest(source, expected_git_dir)
        if problem or after != before:
            return None, problem or "worktree changed while recovery was copied"
        manifest = temporary / "manifest.json"
        manifest.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
        problem = _fsync_tree(temporary)
        if problem:
            return None, problem
        os.rename(str(temporary), str(final))
        descriptor = os.open(str(recovery_root), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record, None
    except FileExistsError:
        problem = validate_snapshot(
            record, unit_id, attempt_id, base_commit, base_tree)
        return (record, None) if not problem else (None, problem)
    except OSError as exc:
        return None, f"cannot publish recovery snapshot for {source}: {exc}"
    finally:
        if temporary.exists():
            shutil.rmtree(str(temporary), ignore_errors=True)


def restore_audit_copy(record, destination):
    """Restore preserved bytes into a new directory; confer no authority."""
    problem = validate_snapshot(record)
    if problem:
        return problem
    destination = Path(destination)
    if destination.exists():
        return f"recovery destination {destination} already exists"
    content = Path(record["snapshot_path"]) / "content"
    try:
        problem = _copy_contents(content, destination)
    except OSError as exc:
        shutil.rmtree(str(destination), ignore_errors=True)
        return f"cannot restore recovery snapshot: {exc}"
    if problem:
        shutil.rmtree(str(destination), ignore_errors=True)
        return problem
    observed, problem = tree_digest(destination)
    if problem:
        return problem
    if observed != record["content_sha256"]:
        shutil.rmtree(str(destination), ignore_errors=True)
        return "restored recovery bytes failed their content digest"
    return None
