"""Remove identifying and secret-looking text. This is the SECOND layer.

The first layer is the report schema: it only contains allowlisted, typed
fields, never raw log lines, command lines, or excerpts. This module is the
safety net for anything that slipped into a string anyway.

Redaction is deliberately aggressive and has no off switch for shareable
reports. A false positive costs a little detail; a miss leaks a secret.

`redact(redact(x)) == redact(x)` must hold, and is used as a guard: if a second
pass still changes the output, something sensitive remained.
"""

import getpass
import ipaddress
import re
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Well-known public resolvers name no host, and our own advice mentions them.
PUBLIC_RESOLVERS = frozenset({"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"})
IP = "[redacted-ip]"
KEY = "[redacted-key]"
SECRET = "[redacted]"
ID = "[redacted-id]"
HOST = "[host]"
USER = "[user]"
EMAIL = "[redacted-email]"
MAC = "[redacted-mac]"

_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEYVALUE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|credential|auth)"
    r"[A-Za-z0-9_.-]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)
_TOKENS = re.compile(
    r"\bhf_[A-Za-z0-9]{20,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_UUID = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
_IPV6 = re.compile(
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[\w.-]+)?(?![\w:])"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = re.compile(rf"(?<![\w.])(?:{_OCTET}\.){{3}}{_OCTET}(?![\w])")
_HOME = re.compile(r"(/home/|/Users/)[^/\s'\"]+")
_WINHOME = re.compile(r"([A-Za-z]:\\Users\\)[^\\\s'\"]+")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_OPAQUE = re.compile(
    r"\b(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*\d)"
    r"[A-Za-z0-9+/_-]{40,}={0,2}(?![\w])"
)


def _ipv6(match: re.Match[str]) -> str:
    text = match.group(0)
    try:
        ipaddress.IPv6Address(text.split("%", 1)[0])
    except ValueError:
        return text  # a time such as 10:15:42, not an address
    return IP


class Redactor:
    """Redacts text. Known host identifiers are removed by value as well."""

    def __init__(
        self,
        hostnames: Iterable[str] = (),
        usernames: Iterable[str] = (),
        machine_ids: Iterable[str] = (),
    ) -> None:
        self._hosts = self._names(hostnames, minimum=2)
        self._users = self._names(usernames, minimum=3)
        self._ids = [m.strip() for m in machine_ids if len(m.strip()) >= 8]

    @staticmethod
    def _names(values: Iterable[str], minimum: int) -> list[str]:
        found: set[str] = set()
        for value in values:
            value = value.strip()
            if len(value) >= minimum:
                found.add(value)
                head = value.split(".", 1)[0]  # the short name of a full host name
                if len(head) >= minimum:
                    found.add(head)
        return sorted(found, key=len, reverse=True)  # longest first

    def text(self, text: str) -> str:
        out = _PEM.sub(KEY, text)
        out = _BEARER.sub(lambda m: f"{m.group(1)} {SECRET}", out)
        out = _KEYVALUE.sub(lambda m: f"{m.group(1)}={SECRET}", out)
        out = _TOKENS.sub(KEY, out)
        out = _EMAIL.sub(EMAIL, out)
        out = _MAC.sub(MAC, out)
        out = _UUID.sub(ID, out)
        out = _IPV6.sub(_ipv6, out)
        out = _IPV4.sub(
            lambda m: m.group(0) if m.group(0) in PUBLIC_RESOLVERS else IP, out
        )
        for machine_id in self._ids:
            out = out.replace(machine_id, ID)
        for host in self._hosts:
            out = re.sub(
                rf"(?<![\w.-]){re.escape(host)}(?![\w-])",
                HOST,
                out,
                flags=re.IGNORECASE,
            )
        out = _HOME.sub(lambda m: m.group(1) + USER, out)
        out = _WINHOME.sub(lambda m: m.group(1) + USER, out)
        for user in self._users:
            out = re.sub(
                rf"(?<![\w-]){re.escape(user)}(?![\w-])", USER, out, flags=re.IGNORECASE
            )
        out = _HEX.sub(ID, out)
        return _OPAQUE.sub(KEY, out)

    def obj(self, data: Any) -> Any:
        """Redact every string value (not keys) in nested dicts and lists."""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, dict):
            return {key: self.obj(value) for key, value in data.items()}
        if isinstance(data, list | tuple):
            return [self.obj(item) for item in data]
        return data

    def leaves_something(self, text: str) -> bool:
        """True if a second pass still changes the text: the first was incomplete."""
        return self.text(text) != text


def local_identifiers() -> tuple[list[str], list[str], list[str]]:
    """This host's name, user name, and machine id, so they can be removed by
    value. Read only to scrub them; they are never put in a report."""
    hosts: list[str] = []
    for getter in (socket.gethostname, socket.getfqdn):
        try:
            hosts.append(getter())
        except OSError:
            pass
    users: list[str] = []
    try:
        users.append(getpass.getuser())
    except OSError, KeyError, ImportError:
        pass
    ids: list[str] = []
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            ids.append(Path(path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            pass
    return hosts, users, ids


def default_redactor() -> Redactor:
    hosts, users, ids = local_identifiers()
    return Redactor(hosts, users, ids)
