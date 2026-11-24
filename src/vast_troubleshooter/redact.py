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
_SECRET_WORD = re.compile(
    r"(?i)api[_-]?key|secret|token|passw(?:or)?d|pwd|credential|auth"
)
# A name followed by ':' or '='. Finding the name run once and only then (if it
# has a secret word) reading the value keeps this linear on long inputs.
_KEYNAME = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)(?=\s*[:=])")
_VALUE = re.compile(r"\s*[:=]\s*(?:\"[^\"]{0,4000}\"|'[^']{0,4000}'|\S+)")
_TOKENS = re.compile(
    r"hf_[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_UUID = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
    r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
_IPV6_MAPPED = re.compile(  # ::ffff:10.0.0.5 and similar
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,6}(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"
)
_IPV6 = re.compile(
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[\w.-]+)?(?![\w:])"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
# Only digits may not touch it: `vm_10.0.0.5_data` is an address with letters
# glued on, but `256.1.1.1` or `1.2.3.4567` is a number, not an address.
_IPV4 = re.compile(rf"(?<!\d)(?:{_OCTET}\.){{3}}{_OCTET}(?!\d)")
_HOME = re.compile(r"(/home/|/Users/)[^/\s'\"]+")
_WINHOME = re.compile(r"([A-Za-z]:\\Users\\)[^\\\s'\"]+")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_OPAQUE = re.compile(
    r"\b(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*\d)"
    r"[A-Za-z0-9+/_-]{40,}={0,2}(?![\w])"
)


_PLACEHOLDER = re.compile(
    r"\[(?:redacted-ip|redacted-key|redacted|redacted-id|redacted-email|redacted-mac|host|user)\]"
)


def _replace_name(text: str, name: str, replacement: str) -> str:
    """Replace a known host or user name wherever it is a separate word.

    A separator is anything except a letter or digit, so `user_alice` and
    `my-alice-box` are caught. Our own placeholders are skipped: a host named
    `host` must not turn `[host]` into `[[host]]`, or the result would change on
    a second pass and the report guard would refuse it. Any other bracketed
    text, such as `[alice]`, is still redacted.
    """
    spans = [m.span() for m in _PLACEHOLDER.finditer(text)]

    def swap(match: re.Match[str]) -> str:
        start = match.start()
        if any(a <= start < b for a, b in spans):
            return match.group(0)
        return replacement

    pattern = rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"
    return re.sub(pattern, swap, text, flags=re.IGNORECASE)


def _redact_keyvalues(text: str) -> str:
    """Replace the value of every `name=value` whose name has a secret word in it.

    A name without one is left alone, and scanning resumes right after the
    name, so a secret later in the same token (`a=password=x`) is still found.
    """
    out: list[str] = []
    pos = 0
    while match := _KEYNAME.search(text, pos):
        name = match.group(1)
        value = _VALUE.match(text, match.end(1)) if _SECRET_WORD.search(name) else None
        if value:
            out.append(text[pos : match.start()])
            out.append(f"{name}={SECRET}")
            pos = value.end()
        else:
            out.append(text[pos : match.end(1)])
            pos = match.end(1)
    out.append(text[pos:])
    return "".join(out)


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
        out = _redact_keyvalues(out)
        out = _TOKENS.sub(KEY, out)
        out = _EMAIL.sub(EMAIL, out)
        out = _MAC.sub(MAC, out)
        out = _UUID.sub(ID, out)
        out = _IPV6_MAPPED.sub(_ipv6, out)
        out = _IPV6.sub(_ipv6, out)
        out = _IPV4.sub(
            lambda m: m.group(0) if m.group(0) in PUBLIC_RESOLVERS else IP, out
        )
        for machine_id in self._ids:
            out = out.replace(machine_id, ID)
        for host in self._hosts:
            out = _replace_name(out, host, HOST)
        out = _HOME.sub(lambda m: m.group(1) + USER, out)
        out = _WINHOME.sub(lambda m: m.group(1) + USER, out)
        for user in self._users:
            out = _replace_name(out, user, USER)
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
