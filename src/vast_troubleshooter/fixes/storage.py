"""Where fix state lives, a cross-process lock, and the action log.

Files are created owner-only and never through a symlink. The lock is held by
the operating system, so a crashed run cannot leave it stuck.

The lock serializes this tool's own fix runs on this host. It does NOT stop Vast
from assigning a new workload, and nothing here claims it does.
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from vast_troubleshooter.securefs import check_not_symlink as _check_not_symlink
from vast_troubleshooter.securefs import ensure_private_dir, open_private


def state_dir() -> Path:
    """Fixes need root, so the normal home is a root-owned system directory."""
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "vast-troubleshooter"
        )
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return Path("/var/lib/vast-troubleshooter")
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "vast-troubleshooter"


def _prepare(path: Path) -> None:
    """The state directory must be ours and private, and the file not a symlink."""
    ensure_private_dir(path.parent)
    _check_not_symlink(path)


class LockBusyError(OSError):
    pass


class ProcessLock:
    """Non-blocking, OS-held exclusive lock on a file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        _prepare(self.path)
        fd = open_private(self.path, os.O_RDWR)  # refuses another user's file
        try:
            if sys.platform == "win32":
                import msvcrt

                os.write(fd, b"\n")  # something to lock
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LockBusyError("another fix run holds the lock") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class ActionLog:
    """Append-only JSON lines. Only fixed, reviewed values are ever written:
    ids, commands from the mutation allowlist, statuses, and exit codes. Command
    output is never stored."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.entries: list[dict[str, object]] = []

    def write(self, entry: dict[str, object]) -> None:
        record = {
            "schema_version": 1,
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            **entry,
        }
        if self.path is not None:
            _prepare(self.path)
            line = json.dumps(record, sort_keys=True) + "\n"
            fd = open_private(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                data = memoryview(line.encode("utf-8"))
                while data:
                    data = data[os.write(fd, data) :]
                os.fsync(fd)
            finally:
                os.close(fd)
        self.entries.append(record)
