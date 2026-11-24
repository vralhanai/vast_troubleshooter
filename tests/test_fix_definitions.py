import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vast_troubleshooter.collectors.network import NetworkCollector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.fixes import (
    FIXES,
    MUTATION_OPERATIONS,
    ActionLog,
    FixEngine,
    FixStatus,
    default_fix_engine,
)
from vast_troubleshooter.fixes.base import OperandsUnavailable
from vast_troubleshooter.fixes.definitions import (
    FIX_DNS_RESOLVER,
    LINK,
    RESTART_VAST_DAEMON,
    _default_link,
    _dns_precheck,
    _restart_precheck,
)
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.rentals import RentalAssessment, RentalState, SourceReading
from vast_troubleshooter.rule_engine import load_default_rules
from vast_troubleshooter.runner import CommandResult, CommandStatus, SubprocessRunner
from vast_troubleshooter.testing import (
    FakeFiles,
    FakeMutationRunner,
    FakeNetwork,
    FakeRunner,
    fake_context,
)

FIX = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
RULES = load_default_rules(known_fix_ids=FIXES)
ROUTE = "default via 10.0.0.1 dev ens3 proto dhcp src 10.0.0.5 metric 100\n"


def ok(op: str, text: str) -> CommandResult:
    return CommandResult(op, CommandStatus.OK, returncode=0, stdout=text)


def facts(**values: object) -> dict[str, Fact]:
    return {
        k.replace("__", "."): Fact(collector="x", key=k.replace("__", "."), value=v)
        for k, v in values.items()
    }


def none_rental(ctx: HostContext) -> RentalAssessment:
    n = RentalState.NONE
    return RentalAssessment(n, (SourceReading("containers", n, "t", T0),), T0, "t")


def active_rental(ctx: HostContext) -> RentalAssessment:
    a = RentalState.ACTIVE
    return RentalAssessment(
        a, (SourceReading("containers", a, "t", T0),), T0, "a container exists"
    )


# -- the definitions are complete and narrow -----------------------------------------------------


def test_the_registry_holds_exactly_the_first_set() -> None:
    assert set(FIXES) == {"restart-vast-daemon", "fix-dns-resolver"}


@pytest.mark.parametrize("fix", [RESTART_VAST_DAEMON, FIX_DNS_RESOLVER])
def test_every_fix_states_target_change_undo_and_a_check(fix) -> None:
    assert fix.target and fix.expected_change and fix.undo_notes and fix.description
    assert fix.resolves_rule and fix.affected_collectors and fix.precheck
    assert (
        fix.requires_root and fix.requires_no_active_rental and fix.settle_seconds > 0
    )


def test_the_restart_fix_is_live_has_no_rollback_and_says_so() -> None:
    assert not RESTART_VAST_DAEMON.demo_only
    assert RESTART_VAST_DAEMON.rollback == ()
    assert "cannot be undone" in RESTART_VAST_DAEMON.undo_notes
    assert RESTART_VAST_DAEMON.settle_seconds >= 5
    assert RESTART_VAST_DAEMON.warnings


def test_the_dns_fix_is_demo_only_reversible_and_warns_about_recovery() -> None:
    assert FIX_DNS_RESOLVER.demo_only
    assert FIX_DNS_RESOLVER.rollback
    text = " ".join(FIX_DNS_RESOLVER.warnings).lower()
    assert "console" in text and "split dns" in text and "disposable" in text
    assert "runtime" in FIX_DNS_RESOLVER.description
    assert "not written to any file" in FIX_DNS_RESOLVER.target


def test_each_rule_that_names_a_fix_points_at_a_fix_that_resolves_it() -> None:
    by_rule = {fix.resolves_rule: fix.id for fix in FIXES.values()}
    for rule in RULES:
        if rule.fix_id:
            assert by_rule[rule.id] == rule.fix_id


def test_every_fix_uses_only_registered_mutation_operations() -> None:
    for fix in FIXES.values():
        for step in (*fix.commands, *fix.rollback):
            assert step.op_id in MUTATION_OPERATIONS


# -- the mutation operations --------------------------------------------------------------------


def test_every_mutation_operation_needs_root_and_has_no_shell_in_it() -> None:
    for op in MUTATION_OPERATIONS.values():
        assert op.needs_root
        assert op.executable in {"systemctl", "resolvectl"}
        for arg in op.args:
            assert not set(arg) & set(";|&$`<>\n ")


def test_the_registries_are_disjoint_and_neither_runner_can_use_the_other() -> None:
    assert not set(MUTATION_OPERATIONS) & set(READ_OPERATIONS)
    read = SubprocessRunner(READ_OPERATIONS)
    mutation = SubprocessRunner(MUTATION_OPERATIONS)
    assert read.run("vast.restart_service").status is CommandStatus.REFUSED
    assert mutation.run("system.uname_release").status is CommandStatus.REFUSED


def planner(is_root: bool = True) -> SubprocessRunner:
    return SubprocessRunner(
        MUTATION_OPERATIONS,
        is_root=lambda: is_root,
        which=lambda name, path: f"/usr/bin/{name}",
    )


def test_the_exact_commands_a_dry_run_would_show() -> None:
    p = planner()
    assert p.plan("vast.restart_service")[0] == [
        "/usr/bin/systemctl",
        "restart",
        "vastai.service",
    ]
    assert p.plan("dns.set_servers", link="ens3")[0] == [
        "/usr/bin/resolvectl",
        "dns",
        "ens3",
        "1.1.1.1",
        "8.8.8.8",
    ]
    assert p.plan("dns.revert_link", link="ens3")[0] == [
        "/usr/bin/resolvectl",
        "revert",
        "ens3",
    ]
    assert p.plan("dns.flush_caches")[0] == ["/usr/bin/resolvectl", "flush-caches"]


def test_without_root_the_mutations_report_needs_root_and_run_nothing() -> None:
    early = planner(is_root=False).plan("vast.restart_service")[1]
    assert early is not None and early.status is CommandStatus.NEEDS_ROOT


def test_with_sudo_allowed_the_prefix_is_fixed() -> None:
    runner = SubprocessRunner(
        MUTATION_OPERATIONS,
        allow_elevated=True,
        is_root=lambda: False,
        which=lambda n, p: f"/usr/bin/{n}",
    )
    assert runner.plan("vast.restart_service")[0][:3] == ["/usr/bin/sudo", "-n", "--"]


@pytest.mark.parametrize(
    "bad", ["eth0; reboot", "", "a" * 16, "-x", "../etc", "eth 0", "eth0\nx"]
)
def test_a_link_name_must_be_a_plain_interface_name(bad: str) -> None:
    assert planner().plan("dns.set_servers", link=bad)[1] is not None
    assert not LINK.fullmatch(bad)


# -- preconditions -----------------------------------------------------------------------------------


def test_restart_precheck() -> None:
    assert _restart_precheck(facts(vast_host__unit_loaded=True)) is None
    assert "not installed" in _restart_precheck(facts(vast_host__unit_loaded=False))
    assert "could not be checked" in _restart_precheck({})


def good_dns() -> dict[str, Fact]:
    return facts(
        network__resolved_active=True,
        network__nameserver_count=1,
        network__nameserver_loopback_count=1,
    )


def test_dns_precheck_accepts_only_the_plain_systemd_resolved_stub() -> None:
    assert _dns_precheck(good_dns()) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"network__resolved_active": False},
        {"network__nameserver_count": 2},
        {"network__nameserver_count": 0},
        {"network__nameserver_loopback_count": 0},
    ],
)
def test_dns_precheck_refuses_other_setups(changes: dict[str, object]) -> None:
    merged = {k: f.value for k, f in good_dns().items()}
    merged.update({k.replace("__", "."): v for k, v in changes.items()})
    assert (
        _dns_precheck(facts(**{k.replace(".", "__"): v for k, v in merged.items()}))
        is not None
    )


def test_dns_precheck_refuses_when_it_cannot_tell() -> None:
    assert _dns_precheck({}) is not None
    unreadable = {
        "network.resolved_active": Fact(
            collector="n", key="network.resolved_active", status=FactStatus.ERROR
        )
    }
    assert _dns_precheck(unreadable) is not None


# -- the target of the DNS fix ----------------------------------------------------------------------------


# What `resolvectl dns/domain <link>` print for a link with no settings of its own.
CLEAN_LINK = {
    "dns.link_servers": "Link 2 (ens3):\n",
    "dns.link_domains": "Link 2 (ens3):\n",
}


def route_ctx(text: str, status: CommandStatus = CommandStatus.OK) -> HostContext:
    return fake_context(
        results={
            **CLEAN_LINK,
            "dns.default_route": CommandResult(
                "dns.default_route", status, returncode=0, stdout=text
            ),
        }
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ROUTE, {"link": "ens3"}),
        (ROUTE + ROUTE, {"link": "ens3"}),  # the same link twice is still one link
        ("", None),  # no default route
        (
            ROUTE + "default via 10.1.0.1 dev wg0 metric 50\n",
            None,
        ),  # two links: ambiguous
        ("default via 1.1.1.1 dev eth0;reboot\n", None),  # not a plain name
    ],
)
def test_the_default_link(text: str, expected: dict[str, str] | None) -> None:
    got = _default_link(route_ctx(text))
    assert (dict(got) if got else None) == expected


def test_the_default_link_is_unknown_if_the_command_fails() -> None:
    assert _default_link(route_ctx("", CommandStatus.MISSING)) is None


# -- the network collector's new fact -----------------------------------------------------------------------


def resolved(result: CommandResult) -> Fact:
    ctx = fake_context(
        results={"network.resolved_active": result}, network=FakeNetwork()
    )
    return {f.key: f for f in NetworkCollector().collect(ctx)}[
        "network.resolved_active"
    ]


def test_resolved_active_reads_is_active_output_even_when_it_exits_non_zero() -> None:
    assert resolved(ok("network.resolved_active", "active\n")).value is True
    inactive = CommandResult(
        "network.resolved_active",
        CommandStatus.ERROR,
        returncode=3,
        stdout="inactive\n",
    )
    fact = resolved(inactive)
    assert fact.value is False and fact.status is FactStatus.OK


def test_resolved_active_when_systemctl_is_missing_or_odd() -> None:
    assert (
        resolved(CommandResult("network.resolved_active", CommandStatus.MISSING)).status
        is FactStatus.MISSING
    )
    odd = CommandResult(
        "network.resolved_active",
        CommandStatus.ERROR,
        returncode=1,
        stderr="Failed to connect to bus",
    )
    assert resolved(odd).status is FactStatus.ERROR


# -- restart-vast-daemon, end to end through the engine with the real rules --------------------------------


def show(name: str) -> CommandResult:
    return ok("vast.service_show", (FIX / "vast_host" / name).read_text())


def restart_engine(tmp_path: Path, state: str, rental=none_rental, flips: bool = True):
    read = fake_context(
        results={
            "vast.service_show": show(state),
            "vast.daemon_log_tail": ok("vast.daemon_log_tail", ""),
        }
    )

    def on_run(op_id: str) -> None:
        if flips:
            read.runner._results["vast.service_show"] = show("show_active.txt")  # type: ignore[attr-defined]

    mutation = FakeMutationRunner(
        {"vast.restart_service": ["/usr/bin/systemctl", "restart", "vastai.service"]},
        on_run=on_run,
    )
    engine = FixEngine(
        FIXES,
        mutation,
        read,
        RULES,
        log=ActionLog(tmp_path / "a.jsonl"),
        lock_path=tmp_path / "l",
        rental_check=rental,
        clock=lambda: T0,
        sleep=lambda s: None,
    )
    return engine, mutation


def test_restart_dry_run_shows_the_exact_command_and_changes_nothing(
    tmp_path: Path,
) -> None:
    engine, mutation = restart_engine(tmp_path, "show_inactive.txt")
    outcome = engine.run("restart-vast-daemon")
    assert outcome.status is FixStatus.DRY_RUN
    assert outcome.commands == (("/usr/bin/systemctl", "restart", "vastai.service"),)
    assert outcome.before == "fail"
    assert mutation.run_calls == []


def test_restart_applies_and_is_confirmed_by_the_service_being_active(
    tmp_path: Path,
) -> None:
    engine, mutation = restart_engine(tmp_path, "show_failed.txt")
    outcome = engine.run(
        "restart-vast-daemon", apply=True, confirmation="restart-vast-daemon"
    )
    assert outcome.status is FixStatus.SUCCEEDED
    assert (outcome.before, outcome.after) == ("fail", "pass")
    assert mutation.run_calls == ["vast.restart_service"]
    assert not outcome.rolled_back  # there is nothing to roll back, and it says so


def test_restart_that_does_not_bring_the_service_up_is_a_failure(
    tmp_path: Path,
) -> None:
    engine, _ = restart_engine(tmp_path, "show_restart_loop.txt", flips=False)
    outcome = engine.run(
        "restart-vast-daemon", apply=True, confirmation="restart-vast-daemon"
    )
    assert outcome.status is FixStatus.FAILED
    assert "still present" in outcome.reasons[0]


def test_restart_is_not_offered_for_a_service_that_is_already_running(
    tmp_path: Path,
) -> None:
    engine, mutation = restart_engine(tmp_path, "show_active.txt")
    outcome = engine.run(
        "restart-vast-daemon", apply=True, confirmation="restart-vast-daemon"
    )
    assert outcome.block_code == "no_problem"
    assert mutation.run_calls == []


def test_restart_is_not_offered_when_the_unit_is_not_installed(tmp_path: Path) -> None:
    engine, mutation = restart_engine(tmp_path, "show_not_found.txt")
    outcome = engine.run(
        "restart-vast-daemon", apply=True, confirmation="restart-vast-daemon"
    )
    assert outcome.block_code == "precondition_failed"
    assert "not installed" in outcome.reasons[0]
    assert mutation.run_calls == []


def test_restart_is_blocked_by_an_active_rental_even_when_confirmed(
    tmp_path: Path,
) -> None:
    engine, mutation = restart_engine(tmp_path, "show_failed.txt", rental=active_rental)
    outcome = engine.run(
        "restart-vast-daemon", apply=True, confirmation="restart-vast-daemon"
    )
    assert outcome.block_code == "rental_active"
    assert mutation.run_calls == []


def test_restart_is_blocked_with_the_real_rental_check_because_vm_detection_is_a_stub(
    tmp_path: Path,
) -> None:
    read = fake_context(
        results={
            "vast.service_show": show("show_failed.txt"),
            "rentals.docker_ps_all": ok("rentals.docker_ps_all", ""),
        }
    )
    mutation = FakeMutationRunner({"vast.restart_service": ["/x"]})
    engine = FixEngine(
        FIXES,
        mutation,
        read,
        RULES,
        log=ActionLog(tmp_path / "a.jsonl"),
        lock_path=tmp_path / "l",
    )
    outcome = engine.run(
        "restart-vast-daemon", apply=True, confirmation="restart-vast-daemon"
    )
    assert outcome.block_code == "rental_unknown"
    assert mutation.run_calls == []


# -- fix-dns-resolver, end to end (demo only) -------------------------------------------------------------------


def dns_engine(
    tmp_path: Path,
    *,
    nameservers: str = "nameserver 127.0.0.53\n",
    route: str = ROUTE,
    flips: bool = True,
):
    net = FakeNetwork(socket.gaierror(-2, "no"))
    read = fake_context(
        results={
            "network.resolved_active": ok("network.resolved_active", "active\n"),
            "dns.default_route": ok("dns.default_route", route),
            **CLEAN_LINK,
        },
        files={"/etc/resolv.conf": nameservers},
        network=net,
    )

    def on_run(op_id: str) -> None:
        if flips and op_id == "dns.set_servers":
            net._resolve = ["203.0.113.7"]  # type: ignore[attr-defined]

    ops = {op: ["/usr/bin/resolvectl", op] for op in MUTATION_OPERATIONS}
    mutation = FakeMutationRunner(ops, on_run=on_run)
    engine = FixEngine(
        FIXES,
        mutation,
        read,
        RULES,
        log=ActionLog(tmp_path / "a.jsonl"),
        lock_path=tmp_path / "l",
        rental_check=none_rental,
        clock=lambda: T0,
        sleep=lambda s: None,
    )
    return engine, mutation


def confirm() -> dict:
    return {"apply": True, "confirmation": "fix-dns-resolver", "allow_demo": True}


def test_dns_is_refused_without_the_demo_flag(tmp_path: Path) -> None:
    engine, mutation = dns_engine(tmp_path)
    assert (
        engine.run(
            "fix-dns-resolver", apply=True, confirmation="fix-dns-resolver"
        ).block_code
        == "demo_only"
    )
    assert mutation.run_calls == []


def test_dns_fix_succeeds_on_a_plain_resolved_host_and_acts_on_the_default_link(
    tmp_path: Path,
) -> None:
    engine, mutation = dns_engine(tmp_path)
    outcome = engine.run("fix-dns-resolver", **confirm())
    assert outcome.status is FixStatus.SUCCEEDED
    assert mutation.run_calls == ["dns.set_servers", "dns.flush_caches"]
    assert mutation.run_operands == [{"link": "ens3"}, {}]
    assert not outcome.rolled_back


def test_dns_fix_rolls_back_when_the_name_still_does_not_resolve(
    tmp_path: Path,
) -> None:
    engine, mutation = dns_engine(tmp_path, flips=False)
    outcome = engine.run("fix-dns-resolver", **confirm())
    assert outcome.status is FixStatus.FAILED
    assert outcome.rolled_back
    assert mutation.run_calls == [
        "dns.set_servers",
        "dns.flush_caches",
        "dns.revert_link",
        "dns.flush_caches",
    ]
    assert mutation.run_operands[2] == {
        "link": "ens3"
    }  # reverted the same link it changed


@pytest.mark.parametrize(
    "nameservers",
    [
        "nameserver 10.0.0.2\n",
        "nameserver 127.0.0.53\nnameserver 10.0.0.2\n",
        "# none\n",
    ],
)
def test_dns_fix_leaves_other_resolver_setups_to_the_operator(
    tmp_path: Path, nameservers: str
) -> None:
    engine, mutation = dns_engine(tmp_path, nameservers=nameservers)
    outcome = engine.run("fix-dns-resolver", **confirm())
    assert outcome.block_code == "precondition_failed"
    assert "by hand" in outcome.reasons[0]
    assert mutation.run_calls == []


@pytest.mark.parametrize("route", ["", ROUTE + "default via 10.1.0.1 dev wg0\n"])
def test_dns_fix_needs_one_unambiguous_link(tmp_path: Path, route: str) -> None:
    engine, mutation = dns_engine(tmp_path, route=route)
    assert (
        engine.run("fix-dns-resolver", **confirm()).block_code == "operands_unavailable"
    )
    assert mutation.run_calls == []


def test_dns_fix_is_blocked_by_an_unknown_rental(tmp_path: Path) -> None:
    engine, mutation = dns_engine(tmp_path)
    engine._rental_check = lambda ctx: RentalAssessment(
        RentalState.UNKNOWN, (), T0, "vm source is a stub"
    )  # type: ignore[attr-defined]
    assert engine.run("fix-dns-resolver", **confirm()).block_code == "rental_unknown"
    assert mutation.run_calls == []


# -- the default engine ------------------------------------------------------------------------------------------


def test_the_default_engine_builds_and_only_knows_mutations(tmp_path: Path) -> None:
    ctx = fake_context()
    engine = default_fix_engine(ctx)
    assert engine._runner.run("system.uname_release").status is CommandStatus.REFUSED  # type: ignore[attr-defined]


def test_a_read_runner_is_never_given_to_the_fix_engine() -> None:
    assert isinstance(FakeRunner({}), FakeRunner) and not hasattr(
        FakeRunner({}), "plan"
    )
    assert FakeFiles({}).read_text.__name__ == "read_text"


# -- the DNS fix never replaces settings it could not restore (F-05c) -----------------------------


def _link_ctx(servers: str, domains: str = "Link 2 (ens3):\n") -> HostContext:
    return fake_context(
        results={
            "dns.default_route": ok("dns.default_route", ROUTE),
            "dns.link_servers": servers,
            "dns.link_domains": domains,
        }
    )


@pytest.mark.parametrize(
    ("servers", "domains"),
    [
        ("Link 2 (ens3): 192.0.2.1\n", "Link 2 (ens3):\n"),  # its own server
        ("Link 2 (ens3):\n", "Link 2 (ens3): corp.invalid\n"),  # its own search domain
        ("Link 2 (ens3):\n", "Link 2 (ens3): ~.\n"),  # a routing domain
        (
            "garbage with no colon\n",
            "Link 2 (ens3):\n",
        ),  # unparseable: assume the worst
    ],
)
def test_a_link_with_its_own_dns_settings_is_left_to_the_operator(
    servers: str, domains: str
) -> None:
    with pytest.raises(OperandsUnavailable):
        _default_link(_link_ctx(servers, domains))


def test_a_link_whose_settings_cannot_be_read_is_left_to_the_operator() -> None:
    ctx = fake_context(
        results={
            "dns.default_route": ok("dns.default_route", ROUTE),
            "dns.link_servers": CommandResult(
                "dns.link_servers", CommandStatus.ERROR, returncode=1
            ),
            "dns.link_domains": "Link 2 (ens3):\n",
        }
    )
    with pytest.raises(OperandsUnavailable, match="cannot read"):
        _default_link(ctx)


def test_a_link_with_no_settings_of_its_own_is_accepted() -> None:
    assert dict(_default_link(_link_ctx("Link 2 (ens3):\n")) or {}) == {"link": "ens3"}
