"""Small helpers for creating files that hold host data: owner-only, never
through a symlink, never overwriting an existing file.

Existing directories and files are accepted only if they belong to this user (or
to root) and other users cannot write to them. Otherwise another user could swap
or read what this tool writes.
"""

import os
import stat
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_POSIX = os.name == "posix"


def check_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"refusing to use a symlink: {path.name}")


def _trusted_owner(uid: int) -> bool:
    """Owned by the current user or by root."""
    return not _POSIX or uid in {os.geteuid(), 0}  # type: ignore[attr-defined,unused-ignore]


def _mkdir_private(path: Path) -> None:
    """Create the directory and any missing parents, each owner-only (0700).

    `Path.mkdir(parents=True, mode=...)` applies the mode to the last directory
    only, so a missing parent would otherwise be created 0755.
    """
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        check_not_symlink(directory)
        directory.mkdir(mode=0o700)


def ensure_private_dir(path: Path) -> Path:
    """Create the directory owner-only, or accept an existing real one.

    An existing directory must be a real directory (not a symlink), owned by this
    user or root, that other users cannot write to.
    """
    check_not_symlink(path)
    if path.exists():
        if not path.is_dir():
            raise OSError(f"not a directory: {path.name}")
        info = path.stat()
        if _POSIX and info.st_mode & 0o022:
            raise OSError(f"directory is writable by other users: {path.name}")
        if not _trusted_owner(info.st_uid):
            raise OSError(f"directory belongs to another user: {path.name}")
        return path
    if path.parent.exists():
        check_not_symlink(path.parent)
    _mkdir_private(path)
    return path


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def create_private_file(path: Path, data: bytes) -> None:
    """Create a NEW owner-only file. Fails if the path already exists.

    If writing fails part way, the file this call created is removed, so a
    failure never leaves a truncated file behind.
    """
    check_not_symlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)


def open_private(path: Path, flags: int) -> int:
    """Open (creating if needed) a file that must be private to this user.

    Refuses a symlink, a file that is not a regular file, one owned by another
    user, and one that group or other users can read or write.
    """
    check_not_symlink(path)
    fd = os.open(path, flags | os.O_CREAT | NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {path.name}")
        if _POSIX and info.st_mode & 0o077:
            raise OSError(f"file is accessible by other users: {path.name}")
        if not _trusted_owner(info.st_uid):
            raise OSError(f"file belongs to another user: {path.name}")
    except BaseException:
        os.close(fd)
        raise
    return fd
