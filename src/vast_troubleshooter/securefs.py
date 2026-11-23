"""Small helpers for creating files that hold host data: owner-only, never
through a symlink, never overwriting an existing file."""

import os
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def check_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"refusing to use a symlink: {path.name}")


def ensure_private_dir(path: Path) -> Path:
    """Create the directory owner-only, or accept an existing real one.

    An existing directory must be a real directory (not a symlink) that other
    users cannot write to, so another user cannot swap files under us.
    """
    check_not_symlink(path)
    if path.exists():
        if not path.is_dir():
            raise OSError(f"not a directory: {path.name}")
        if os.name == "posix" and path.stat().st_mode & 0o022:
            raise OSError(f"directory is writable by other users: {path.name}")
        return path
    check_not_symlink(path.parent) if path.parent.exists() else None
    path.mkdir(mode=0o700, parents=True)
    return path


def create_private_file(path: Path, data: bytes) -> None:
    """Create a NEW owner-only file. Fails if the path already exists."""
    check_not_symlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
