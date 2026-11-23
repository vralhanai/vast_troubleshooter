"""Network access for collectors, behind an interface so tests need no network.

Only two things are possible: resolve a name, and open a TCP connection that is
closed straight away. No data is ever sent.
"""

import socket
import threading
from typing import Protocol


class NetworkNotAllowedError(Exception):
    """A collector used the network but the context has none (a bug)."""


class NetworkAccess(Protocol):
    def resolve(self, host: str, timeout: float) -> list[str]:
        """Addresses for `host`. Raises socket.gaierror or TimeoutError."""
        ...

    def connect(self, host: str, port: int, timeout: float) -> None:
        """Open and close a TCP connection. Raises OSError (incl. TimeoutError)."""
        ...


class NoNetwork:
    """The default for a context: any use is a bug, so it fails loudly."""

    def resolve(self, host: str, timeout: float) -> list[str]:
        raise NetworkNotAllowedError("this context has no network access")

    def connect(self, host: str, port: int, timeout: float) -> None:
        raise NetworkNotAllowedError("this context has no network access")


class LocalNetwork:
    def resolve(self, host: str, timeout: float) -> list[str]:
        # getaddrinfo has no timeout, so it runs on a daemon thread that is
        # abandoned if it takes too long (it cannot block interpreter exit).
        box: dict[str, object] = {}

        def work() -> None:
            try:
                infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                box["addresses"] = sorted({str(info[4][0]) for info in infos})
            except OSError as exc:
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"name resolution took longer than {timeout}s")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["addresses"]  # type: ignore[return-value]

    def connect(self, host: str, port: int, timeout: float) -> None:
        with socket.create_connection((host, port), timeout=timeout):
            pass
