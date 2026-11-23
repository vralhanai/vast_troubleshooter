import re
import socket
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vast_troubleshooter import cli
from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.network import (
    DNS_HOST,
    DNS_TIMEOUT,
    PROBES,
    RESOLV_CONF,
    TCP_TIMEOUT,
    NetworkCollector,
    parse_nameservers,
)
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.files import DEFAULT_READABLE_FILES
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.netaccess import (
    LocalNetwork,
    NetworkNotAllowedError,
    NoNetwork,
)
from vast_troubleshooter.runner import CommandResult
from vast_troubleshooter.testing import FakeFiles, FakeNetwork, FakeRunner, fake_context

FIXTURES = Path(__file__).parent / "fixtures" / "network"
CF = ("1.1.1.1", 443)
GOOGLE = ("8.8.8.8", 53)


def resolv(name: str) -> dict[str, str | Exception]:
    return {RESOLV_CONF: (FIXTURES / name).read_text()}


def collect(
    network: FakeNetwork | None = None,
    files: dict[str, str | Exception] | None = None,
    allow_outbound: bool = False,
) -> dict[str, Fact]:
    ctx = fake_context(
        files=resolv("resolv_systemd.conf") if files is None else files,
        network=network or FakeNetwork(),
        allow_outbound=allow_outbound,
    )
    return {f.key: f for f in NetworkCollector().collect(ctx)}


# -- DNS ------------------------------------------------------------------------


def test_dns_resolves() -> None:
    net = FakeNetwork(["203.0.113.7", "203.0.113.8"])
    facts = collect(net)
    assert facts["network.dns_vast_ai_resolves"].value is True
    assert facts["network.dns_vast_ai_address_count"].value == 2
    assert net.resolve_calls == [(DNS_HOST, DNS_TIMEOUT)]  # bounded, one lookup


def test_dns_failure_is_an_observation_with_a_reason_but_no_address() -> None:
    facts = collect(FakeNetwork(socket.gaierror(-2, "Name or service not known")))
    fact = facts["network.dns_vast_ai_resolves"]
    assert fact.value is False
    assert fact.status is FactStatus.OK
    assert "gaierror" in (fact.raw_excerpt or "")
    assert "-2" in (fact.raw_excerpt or "")
    assert "network.dns_vast_ai_address_count" not in facts


def test_dns_hang_is_a_failure_observation() -> None:
    facts = collect(FakeNetwork(TimeoutError("slow")))
    fact = facts["network.dns_vast_ai_resolves"]
    assert fact.value is False
    assert fact.status is FactStatus.OK
    assert "timed out" in (fact.raw_excerpt or "")


def test_dns_empty_answer_is_not_resolving() -> None:
    facts = collect(FakeNetwork([]))
    assert facts["network.dns_vast_ai_resolves"].value is False
    assert facts["network.dns_vast_ai_address_count"].value == 0


def test_resolved_addresses_are_never_stored() -> None:
    facts = collect(FakeNetwork(["203.0.113.7"]))
    assert "203.0.113.7" not in " ".join(f.model_dump_json() for f in facts.values())


# -- resolv.conf ----------------------------------------------------------------


def test_systemd_stub_resolver() -> None:
    facts = collect()
    assert facts["network.nameserver_count"].value == 1
    assert facts["network.nameserver_loopback_count"].value == 1
    assert facts["network.nameserver_public_count"].value == 0
    assert facts["network.nameserver_count"].raw_excerpt == "127.0.0.53"


def test_public_nameservers_and_inline_comments() -> None:
    facts = collect(files=resolv("resolv_public.conf"))
    assert facts["network.nameserver_count"].value == 2
    assert facts["network.nameserver_public_count"].value == 2
    assert facts["network.nameserver_loopback_count"].value == 0


def test_private_and_ipv6_nameservers() -> None:
    facts = collect(files=resolv("resolv_private_and_ipv6.conf"))
    assert facts["network.nameserver_count"].value == 2
    assert facts["network.nameserver_public_count"].value == 1  # only the IPv6 one


def test_no_nameservers_is_a_count_of_zero() -> None:
    facts = collect(files=resolv("resolv_empty.conf"))
    assert facts["network.nameserver_count"].value == 0
    assert facts["network.nameserver_count"].status is FactStatus.OK
    assert facts["network.nameserver_count"].raw_excerpt is None


def test_junk_lines_are_ignored() -> None:
    text = (FIXTURES / "resolv_junk.conf").read_text()
    assert parse_nameservers(text) == ["9.9.9.9"]


def test_missing_resolv_conf() -> None:
    facts = collect(files={})
    assert facts["network.nameserver_count"].status is FactStatus.MISSING
    assert "network.nameserver_loopback_count" not in facts


def test_unreadable_resolv_conf_is_an_error_fact() -> None:
    facts = collect(files={RESOLV_CONF: PermissionError("denied")})
    assert facts["network.nameserver_count"].status is FactStatus.ERROR


def test_nameserver_addresses_stay_out_of_shareable_facts() -> None:
    facts = collect(files=resolv("resolv_public.conf"))
    shareable = " ".join(f.to_shareable().model_dump_json() for f in facts.values())
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", shareable)
    assert "1.1.1.1" in (facts["network.nameserver_count"].raw_excerpt or "")


def test_resolv_conf_is_on_the_file_allowlist() -> None:
    assert RESOLV_CONF in DEFAULT_READABLE_FILES


# -- outbound probes: off by default ---------------------------------------------


def test_no_connection_is_made_by_default() -> None:
    net = FakeNetwork(connect_results={CF: None, GOOGLE: None})
    facts = collect(net)
    assert net.connect_calls == []
    assert facts["network.outbound_probes_enabled"].value is False
    assert not any(k.startswith("network.outbound_c") for k in facts)
    assert not any(k.startswith("network.outbound_g") for k in facts)


def test_enabled_probes_connect_to_exactly_the_listed_destinations() -> None:
    net = FakeNetwork(connect_results={CF: None, GOOGLE: None})
    facts = collect(net, allow_outbound=True)
    assert net.connect_calls == [
        ("1.1.1.1", 443, TCP_TIMEOUT),
        ("8.8.8.8", 53, TCP_TIMEOUT),
    ]
    assert facts["network.outbound_probes_enabled"].value is True
    assert facts["network.outbound_cloudflare_443"].value is True
    assert facts["network.outbound_google_dns_53"].value is True


def test_failed_connections_are_observations() -> None:
    net = FakeNetwork(
        connect_results={CF: ConnectionRefusedError(), GOOGLE: TimeoutError()}
    )
    facts = collect(net, allow_outbound=True)
    refused = facts["network.outbound_cloudflare_443"]
    assert refused.value is False and refused.status is FactStatus.OK
    assert "ConnectionRefusedError" in (refused.raw_excerpt or "")
    assert "timed out" in (facts["network.outbound_google_dns_53"].raw_excerpt or "")


def test_probe_facts_are_safe_to_share() -> None:
    net = FakeNetwork(connect_results={CF: None, GOOGLE: OSError(101, "unreachable")})
    for fact in collect(net, allow_outbound=True).values():
        fact.to_shareable()


def test_notice_lists_destinations_and_what_is_revealed() -> None:
    text = "\n".join(NetworkCollector().outbound_notice())
    for probe in PROBES:
        assert f"{probe.host}:{probe.port}" in text
    assert "No data is sent" in text
    assert "public IP" in text
    assert f"{len(PROBES)} connections" in text


def test_probe_list_is_fixed_and_small() -> None:
    assert [(p.host, p.port) for p in PROBES] == [CF, GOOGLE]


# -- default context has no network ---------------------------------------------


def test_default_context_network_fails_loudly_and_is_isolated_by_run_collectors() -> (
    None
):
    from vast_troubleshooter.collectors import run_collectors

    ctx = HostContext(runner=FakeRunner({}), files=FakeFiles({}))
    assert isinstance(ctx.network, NoNetwork)
    with pytest.raises(NetworkNotAllowedError):
        ctx.network.resolve("vast.ai", 1.0)
    facts = run_collectors([NetworkCollector()], ctx)
    assert facts[0].key == "network.collector"
    assert facts[0].status is FactStatus.ERROR


# -- the real LocalNetwork, using only loopback ----------------------------------


def test_local_network_connects_to_a_loopback_listener() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        LocalNetwork().connect("127.0.0.1", server.getsockname()[1], 2.0)
    finally:
        server.close()


def test_local_network_connection_refused_raises_oserror() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()  # nothing is listening now
    with pytest.raises(OSError):
        LocalNetwork().connect("127.0.0.1", port, 2.0)


def test_local_network_resolves_localhost_without_the_internet() -> None:
    assert LocalNetwork().resolve("localhost", 5.0)


def test_local_network_resolve_is_bounded_by_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def slow(*args: object, **kwargs: object) -> list[object]:
        release.wait(10)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        LocalNetwork().resolve("vast.ai", 0.3)
    assert time.monotonic() - start < 3
    release.set()


# -- CLI ------------------------------------------------------------------------

runner = CliRunner()


def test_collect_network_without_the_flag_makes_no_outbound_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    net = FakeNetwork()

    def make(allow_outbound: bool = False) -> HostContext:
        seen.append(allow_outbound)
        return fake_context(
            files=resolv("resolv_systemd.conf"),
            network=net,
            allow_outbound=allow_outbound,
        )

    monkeypatch.setattr(cli, "default_context", make)
    result = runner.invoke(cli.app, ["collect", "network"])
    assert result.exit_code == 0
    assert seen == [False]
    assert net.connect_calls == []
    assert "Outbound probes are enabled" not in result.output
    assert "network.dns_vast_ai_resolves = True [ok]" in result.stdout


def test_collect_network_with_the_flag_lists_destinations_then_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    net = FakeNetwork(connect_results={CF: None, GOOGLE: None})

    def make(allow_outbound: bool = False) -> HostContext:
        seen.append(allow_outbound)
        return fake_context(
            files=resolv("resolv_systemd.conf"),
            network=net,
            allow_outbound=allow_outbound,
        )

    monkeypatch.setattr(cli, "default_context", make)
    result = runner.invoke(cli.app, ["collect", "network", "--allow-outbound"])
    assert result.exit_code == 0
    assert seen == [True]
    assert "1.1.1.1:443" in result.output and "8.8.8.8:53" in result.output
    assert "network.outbound_cloudflare_443 = True [ok]" in result.stdout
    assert len(net.connect_calls) == 2


def test_flag_does_not_print_a_notice_for_collectors_without_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "default_context", lambda allow_outbound=False: fake_context()
    )
    result = runner.invoke(cli.app, ["collect", "system", "--allow-outbound"])
    assert "Outbound probes are enabled" not in result.output


def test_registry_includes_network() -> None:
    assert "network" in COLLECTORS
    assert isinstance(CommandResult, type)
