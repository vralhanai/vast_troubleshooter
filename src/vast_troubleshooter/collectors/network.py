"""DNS, configured nameservers, and (opt-in) outbound reachability.

By default this only asks the local resolver about vast.ai and reads
/etc/resolv.conf. Connections to third-party servers run only when the context
allows outbound probes, and they send no data: a TCP connect that is closed
immediately.

Nameserver addresses stay in `raw_excerpt` (local only). Shareable facts carry
only counts, so no address leaves the host in a report.
"""

import ipaddress
from dataclasses import dataclass
from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus

DNS_HOST = "vast.ai"
DNS_TIMEOUT = 5.0
TCP_TIMEOUT = 3.0
RESOLV_CONF = "/etc/resolv.conf"


@dataclass(frozen=True)
class OutboundProbe:
    key: str
    host: str
    port: int
    label: str


# A fixed, short list. Adding a destination here is a reviewed change.
PROBES = (
    OutboundProbe(
        "network.outbound_cloudflare_443", "1.1.1.1", 443, "Cloudflare public DNS"
    ),
    OutboundProbe("network.outbound_google_dns_53", "8.8.8.8", 53, "Google Public DNS"),
)


def parse_nameservers(text: str) -> list[str]:
    """Valid `nameserver` addresses from resolv.conf text, in order."""
    found = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split(";", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                found.append(str(ipaddress.ip_address(parts[1])))
            except ValueError:
                continue
    return found


def _why(exc: OSError) -> str:
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else exc.errno
    return f"{type(exc).__name__}" + (f", code {code}" if code is not None else "")


class NetworkCollector(Collector):
    name: ClassVar[str] = "network"

    def outbound_notice(self) -> list[str]:
        lines = ["Outbound probes are enabled. These connections will be made:"]
        for probe in PROBES:
            lines.append(
                f"  {probe.host}:{probe.port} ({probe.label}): TCP connect only. "
                f"No data is sent. The server sees this host's public IP address "
                f"and the time of the connection."
            )
        lines.append(
            f"Each connection waits at most {TCP_TIMEOUT:g}s. "
            f"{len(PROBES)} connections in total."
        )
        return lines

    def collect(self, ctx: HostContext) -> list[Fact]:
        return [*self._dns(ctx), *self._nameservers(ctx), *self._outbound(ctx)]

    # -- DNS ------------------------------------------------------------------

    def _dns(self, ctx: HostContext) -> list[Fact]:
        key = "network.dns_vast_ai_resolves"
        source = f"getaddrinfo {DNS_HOST}"
        # A failure or a hang is an observation about DNS, not an unknown.
        try:
            addresses = ctx.network.resolve(DNS_HOST, DNS_TIMEOUT)
        except TimeoutError:
            return [
                self.fact(
                    key,
                    False,
                    source_command=source,
                    raw_excerpt=f"timed out after {DNS_TIMEOUT:g}s",
                )
            ]
        except OSError as exc:
            return [
                self.fact(
                    key,
                    False,
                    source_command=source,
                    raw_excerpt=f"resolution failed ({_why(exc)})",
                )
            ]
        return [
            self.fact(key, bool(addresses), source_command=source),
            self.fact(
                "network.dns_vast_ai_address_count",
                len(addresses),
                source_command=source,
            ),
        ]

    # -- resolv.conf ----------------------------------------------------------

    def _nameservers(self, ctx: HostContext) -> list[Fact]:
        source = f"read {RESOLV_CONF}"
        try:
            text = ctx.files.read_text(RESOLV_CONF)
        except FileNotFoundError:
            return [
                self.fact(
                    "network.nameserver_count",
                    status=FactStatus.MISSING,
                    source_command=source,
                )
            ]
        except OSError as exc:
            return [
                self.fact(
                    "network.nameserver_count",
                    status=FactStatus.ERROR,
                    source_command=source,
                    raw_excerpt=f"{type(exc).__name__}: {exc}",
                )
            ]
        servers = parse_nameservers(text)
        parsed = [ipaddress.ip_address(s) for s in servers]
        return [
            # The addresses are local-only evidence, never a shareable value.
            self.fact(
                "network.nameserver_count",
                len(servers),
                source_command=source,
                raw_excerpt=", ".join(servers) or None,
            ),
            self.fact(
                "network.nameserver_loopback_count",
                sum(1 for a in parsed if a.is_loopback),
                source_command=source,
            ),
            self.fact(
                "network.nameserver_public_count",
                sum(1 for a in parsed if a.is_global),
                source_command=source,
            ),
        ]

    # -- opt-in outbound probes -----------------------------------------------

    def _outbound(self, ctx: HostContext) -> list[Fact]:
        facts = [self.fact("network.outbound_probes_enabled", ctx.allow_outbound)]
        if not ctx.allow_outbound:
            return facts
        for probe in PROBES:
            source = f"tcp connect {probe.host}:{probe.port}"
            try:
                ctx.network.connect(probe.host, probe.port, TCP_TIMEOUT)
            except TimeoutError:
                facts.append(
                    self.fact(
                        probe.key,
                        False,
                        source_command=source,
                        raw_excerpt=f"timed out after {TCP_TIMEOUT:g}s",
                    )
                )
            except OSError as exc:
                facts.append(
                    self.fact(
                        probe.key, False, source_command=source, raw_excerpt=_why(exc)
                    )
                )
            else:
                facts.append(self.fact(probe.key, True, source_command=source))
        return facts
