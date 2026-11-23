"""Allowlisted, size-bounded file reads for collectors.

Like commands, files are read only if they are on a fixed allowlist. Add a path
here only with a collector that needs it.
"""

from collections.abc import Iterable
from typing import Protocol

MAX_FILE_BYTES = 64 * 1024

DEFAULT_READABLE_FILES = frozenset(
    {
        "/etc/os-release",
        "/proc/uptime",
        "/etc/resolv.conf",
        "/proc/version",
        "/.dockerenv",
        "/run/.containerenv",
    }
)


class FileNotAllowedError(Exception):
    """A collector asked for a path that is not on the allowlist (a bug)."""


class FileSource(Protocol):
    def read_text(self, path: str) -> str:
        """Return the file text. May raise FileNotFoundError or OSError."""
        ...


class LocalFiles:
    def __init__(
        self,
        allowed: Iterable[str] = DEFAULT_READABLE_FILES,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self._allowed = frozenset(allowed)
        self._max_bytes = max_bytes

    def read_text(self, path: str) -> str:
        if path not in self._allowed:
            raise FileNotAllowedError(path)
        with open(path, "rb") as handle:
            data = handle.read(self._max_bytes)
        return data.decode("utf-8", errors="replace")
