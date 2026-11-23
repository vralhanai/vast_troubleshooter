"""The two fixes of the first set, and the mutation operations they use.

Each is deliberately narrow. Anything the tool does not understand is left for
the operator: a refusal with a reason is a good outcome.
"""

import re
from collections.abc import Mapping

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.fixes.base import Fix, FixStep
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import Operation

# -- mutation operations (separate from the read allowlist) ----------------------

# Network link names are at most 15 characters. Nothing else is accepted, and a
# name cannot start with '-' (it would read as an option).
LINK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")  # the examples on the Vast offline page

MUTATION_OPS = (
    Operation(
        id="vast.restart_service",
        executable="systemctl",
        args=("restart", "vastai.service"),
        needs_root=True,
        timeout=90,
    ),
    Operation(
        id="dns.set_servers",
        executable="resolvectl",
        args=("dns", "{link}", *PUBLIC_RESOLVERS),
        operands={"link": LINK},
        needs_root=True,
    ),
    Operation(
        id="dns.flush_caches",
        executable="resolvectl",
        args=("flush-caches",),
        needs_root=True,
    ),
    Operation(
        id="dns.revert_link",
        executable="resolvectl",
        args=("revert", "{link}"),
        operands={"link": LINK},
        needs_root=True,
    ),
)


def _value(facts: Mapping[str, Fact], key: str) -> object:
    fact = facts.get(key)
    return fact.value if fact is not None and fact.status is FactStatus.OK else None


# -- restart-vast-daemon ----------------------------------------------------------


def _restart_precheck(facts: Mapping[str, Fact]) -> str | None:
    loaded = _value(facts, "vast_host.unit_loaded")
    if loaded is not True:
        why = "is not installed" if loaded is False else "could not be checked"
        return f"vastai.service {why}; restarting cannot help"
    return None


RESTART_VAST_DAEMON = Fix(
    id="restart-vast-daemon",
    description="Restart the Vast host daemon (vastai.service).",
    target="the systemd unit vastai.service",
    expected_change="The daemon is stopped and started again and stays running.",
    undo_notes=(
        "A restart cannot be undone. Any workload the restart interrupted is not restored. "
        "That is why it is refused unless no rental can be found."
    ),
    commands=("vast.restart_service",),
    resolves_rule="vast_host.service_not_active",
    affected_collectors=("vast_host",),
    precheck=_restart_precheck,
    settle_seconds=10.0,  # a service that starts and then crashes must not count as fixed
    warnings=(
        "This stops and starts the Vast daemon. Customer workloads can be interrupted.",
    ),
)


# -- fix-dns-resolver (demo only) -----------------------------------------------------


def _dns_precheck(facts: Mapping[str, Fact]) -> str | None:
    if _value(facts, "network.resolved_active") is not True:
        return "systemd-resolved is not the active resolver; other resolver setups are changed by hand"
    if (
        _value(facts, "network.nameserver_count") != 1
        or _value(facts, "network.nameserver_loopback_count") != 1
    ):
        return (
            "/etc/resolv.conf is not the plain systemd-resolved stub "
            "(split DNS, a VPN, or a static file); change DNS by hand"
        )
    return None


def _default_link(ctx: HostContext) -> Mapping[str, str] | None:
    """The one network link carrying the default route, or None if unclear."""
    result = ctx.runner.run("dns.default_route")
    if not result.ok:
        return None
    links = {m.group(1) for m in re.finditer(r"\bdev (\S+)", result.stdout)}
    if len(links) != 1:
        return None  # no default route, or several: ambiguous, so advisory
    link = links.pop()
    return {"link": link} if LINK.fullmatch(link) else None


FIX_DNS_RESOLVER = Fix(
    id="fix-dns-resolver",
    description="Point DNS on the default network link at public resolvers, at runtime only.",
    target="DNS servers of the default network link, held by systemd-resolved (not written to any file)",
    expected_change=f"The link uses {' and '.join(PUBLIC_RESOLVERS)}, and vast.ai resolves.",
    undo_notes="Run: sudo resolvectl revert <link>. The change is runtime-only, so a reboot also undoes it.",
    commands=(
        FixStep("dns.set_servers", ("link",)),
        FixStep("dns.flush_caches"),
    ),
    resolves_rule="network.dns_vast_ai_unresolvable",
    affected_collectors=("network",),
    precheck=_dns_precheck,
    operand_source=_default_link,
    # If DNS is not fixed afterwards, put the link back the way it was.
    rollback=(FixStep("dns.revert_link", ("link",)), FixStep("dns.flush_caches")),
    settle_seconds=2.0,
    demo_only=True,
    warnings=(
        "Demo only: use a disposable test machine, never a live host.",
        "Keep a console or out-of-band way in. A wrong DNS change can cut network access.",
        "Replacing resolvers can break split DNS and leak internal names to public resolvers.",
    ),
)
