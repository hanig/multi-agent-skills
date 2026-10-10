"""Local Codex homes. Coordinator state, never agent files, owns cleanup.

Python 3.9+, standard library only. Same-UID deliberate credential changes
and concurrent filesystem replacement remain the trusted-writer boundary.
"""
import json
import os
import shlex
import shutil
import stat
from pathlib import Path

import coordinator_paths as CP


class HomeError(ValueError):
    pass


def source_home():
    return Path(os.environ.get("HANIG_SWARM_CODEX_AUTH_HOME",
                               str(Path.home() / ".codex-api"))).expanduser().absolute()


def operator_home():
    # Deliberately independent of inherited CODEX_HOME (possibly this agent's
    # private home). This override also keeps tests off the operator's files.
    return Path(os.environ.get("HANIG_SWARM_CODEX_OPERATOR_HOME",
                               str(Path.home() / ".codex"))).expanduser().absolute()


def validate_source(source=None):
    """Return only the auth pathname; discard all parsed fields but auth_mode.

    Never include parser/OS exception text or an observed auth value in the
    diagnostic: those can contain credential bytes from malformed input.
    """
    home = Path(source).expanduser().absolute() if source is not None else source_home()
    auth = home / "auth.json"
    reason = None
    try:
        info = auth.lstat()
        if not stat.S_ISREG(info.st_mode):
            reason = "auth.json must be a regular file, not a symlink"
        elif info.st_uid != os.getuid():
            reason = "auth.json must be owned by the current user"
        elif stat.S_IMODE(info.st_mode) != 0o400:
            reason = ("auth.json must have mode 0400; use a POSIX filesystem "
                      "that stores file modes (ExFAT and FAT report modes "
                      "like 0777 and are safely refused)")
        else:
            fd = os.open(str(auth), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (
                        info.st_dev, info.st_ino, info.st_uid, info.st_mode):
                    reason = "auth.json changed during validation; retry"
                elif json.load(handle).get("auth_mode") != "apikey":
                    reason = 'auth.json must use auth_mode "apikey"'
    except (OSError, ValueError, AttributeError, RecursionError):
        reason = "auth.json is missing, unreadable, or invalid JSON"
    if reason:
        quoted_auth, quoted_home = shlex.quote(str(auth)), shlex.quote(str(home))
        fix = (f"chmod 600 {quoted_auth} 2>/dev/null; printenv OPENAI_API_KEY | "
               f"CODEX_HOME={quoted_home} codex login --with-api-key && "
               f"chmod 400 {quoted_auth}")
        raise HomeError(f"{str(auth)!r}: {reason}; fix: {fix}".replace("\n", "\\n").replace("\r", "\\r"))
    return auth


def home_path(state_dir, attempt):
    if (not isinstance(attempt, str) or not attempt
            or attempt in (".", "..") or Path(attempt).name != attempt):
        raise HomeError("invalid Codex home attempt id")
    return Path(state_dir).resolve() / "codex-homes" / attempt


def _check_parent(home):
    if home.parent.is_symlink():
        raise HomeError("codex-homes must not be a symlink")


def check_effective_uid():
    if os.geteuid() == 0:
        raise HomeError("cannot create Codex home with effective uid 0: root bypasses "
                        "mode 0400; run as a non-root user")


def allocate(state_dir, attempt, unit_dir, unit):
    """Return (home, identity); roll back failed setup before handing off ownership."""
    check_effective_uid()
    home = home_path(state_dir, attempt)
    CP.resolve_paths(state_dir, plan={"units": [unit]})
    _check_parent(home)
    if CP._inside(home, unit_dir):
        raise HomeError("Codex home must be outside the attempt write root")
    if CP.git_top(home.parent if home.parent.exists() else home.parent.parent):
        raise HomeError("Codex home must be outside every Git worktree")
    home_created = False
    created = None
    try:
        parent_created = False
        try:
            home.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            parent_created = True
        except FileExistsError:
            pass
        parent = home.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode):
            raise HomeError("codex-homes must be a real directory")
        # mkdir's mode is filtered by umask, including owner write/search
        # bits. Restore those before attempting to create a child, but never
        # chmod an existing parent owned by somebody else.
        if parent_created or parent.st_uid == os.getuid():
            home.parent.chmod(0o700)
        home.mkdir(mode=0o700, exist_ok=False)
        home_created = True
        created = home.lstat()
        home.chmod(0o700)
    except BaseException as exc:
        if home_created:
            try:
                # mkdir succeeded even if the first identity read failed.
                # Recover that identity without following the leaf, then
                # independently check it before removing the empty directory.
                if created is None:
                    created = os.stat(home, follow_symlinks=False)
                current = os.stat(home, follow_symlinks=False)
                if (not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino) != (
                            created.st_dev, created.st_ino)):
                    raise HomeError("refusing failed Codex home allocation cleanup: "
                                    "directory identity changed")
                # No links have been populated yet. Never recursively remove
                # unexpected contents or a replacement at the allocated path.
                home.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                raise HomeError(f"cannot remove failed Codex home allocation "
                                f"{str(home)!r}") from None
        if isinstance(exc, OSError):
            raise HomeError(f"cannot exclusively create Codex home {str(home)!r}") from None
        raise
    return home, {"device": created.st_dev, "inode": created.st_ino}


def populate(home, auth, operator=None):
    operator = Path(operator) if operator is not None else operator_home()
    try:
        (home / "auth.json").symlink_to(auth)
        for name in ("config.toml", "skills"):
            target = operator / name
            if target.exists():
                (home / name).symlink_to(target)
    except OSError:
        raise HomeError(f"cannot populate Codex home {str(home)!r}") from None


def remove(state_dir, attempt, recorded_path, recorded_identity):
    """Remove exactly a coordinator-recorded home, without resolving its leaf.

    The leaf must still be the real directory recorded at allocation.
    rmtree unlinks nested symlinks without following their targets.
    Missing homes are successful retries after an interrupted state save.
    """
    expected = home_path(state_dir, attempt)
    if not isinstance(recorded_path, str) or recorded_path != str(expected):
        raise HomeError("refusing unrecorded or out-of-root Codex home")
    _check_parent(expected)
    try:
        try:
            info = expected.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise HomeError("refusing Codex home replacement: not a real directory")
        if (not isinstance(recorded_identity, dict)
                or (info.st_dev, info.st_ino) != (
                    recorded_identity.get("device"), recorded_identity.get("inode"))):
            raise HomeError("refusing Codex home replacement: device/inode does not match coordinator state")
        shutil.rmtree(expected)
    except OSError:
        raise HomeError("Codex home removal failed; will retry next advance") from None


def doctor():
    try:
        auth = validate_source()
    except HomeError as exc:
        return "Codex API source: NOT READY -- " + str(exc)
    return f"Codex API source: READY -- {str(auth)!r}, auth_mode=apikey, mode=0400"


if __name__ == "__main__":
    print(doctor())
