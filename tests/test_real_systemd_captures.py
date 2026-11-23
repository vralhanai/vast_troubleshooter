"""The vast_host and network collectors against REAL systemd output.

Captured 2026-11-23 by e2e/run_poc.sh inside an Ubuntu 24.04 container running
systemd as PID 1 (systemd 255). The unit is a stand-in `vastai.service`, not the
real Vast daemon, but `systemctl show` output has the same shape for any unit.
"""

from pathlib import Path

import pytest

from vast_troubleshooter.collectors.network import NetworkCollector
from vast_troubleshooter.collectors.vast_host import VastHostCollector, parse_show
from vast_troubleshooter.models import Fact
from vast_troubleshooter.rule_engine import evaluate_rules, load_default_rules
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeNetwork, fake_context

REAL = Path(__file__).parent / "fixtures" / "real_systemd_container"
RULES = {r.id: r for r in load_default_rules()}


def text(name: str) -> str:
    return (REAL / name).read_text()


def vast_host(name: str) -> dict[str, Fact]:
    ctx = fake_context(
        results={
            "vast.service_show": CommandResult(
                "vast.service_show", CommandStatus.OK, returncode=0, stdout=text(name)
            ),
            "vast.daemon_log_tail": CommandResult(
                "vast.daemon_log_tail", CommandStatus.OK, stdout=""
            ),
        }
    )
    return {f.key: f for f in VastHostCollector().collect(ctx)}


def test_real_output_lists_nrestarts_first_so_parsing_must_not_depend_on_order() -> (
    None
):
    assert text("systemctl_show_vastai_active.txt").splitlines()[0] == "NRestarts=0"
    assert parse_show(text("systemctl_show_vastai_active.txt")) == {
        "NRestarts": "0",
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
    }


def test_a_running_service() -> None:
    facts = vast_host("systemctl_show_vastai_active.txt")
    assert facts["vast_host.unit_loaded"].value is True
    assert facts["vast_host.service_active"].value is True
    assert facts["vast_host.sub_state"].value == "running"
    assert facts["vast_host.nrestarts"].value == 0
    assert facts["vast_host.restart_loop"].value is False


def test_a_stopped_service() -> None:
    facts = vast_host("systemctl_show_vastai_inactive.txt")
    assert facts["vast_host.service_active"].value is False
    assert facts["vast_host.active_state"].value == "inactive"
    assert facts["vast_host.restart_loop"].value is False


def test_real_auto_restart_is_a_restart_loop_and_reads_as_activating() -> None:
    """The case the Vast offline page describes: the unit failed and systemd is waiting
    to start it again. Real systemd reports ActiveState=activating, SubState=auto-restart."""
    facts = vast_host("systemctl_show_vastai_autorestart.txt")
    assert facts["vast_host.active_state"].value == "activating"
    assert facts["vast_host.sub_state"].value == "auto-restart"
    assert facts["vast_host.restart_loop"].value is True
    assert facts["vast_host.service_active"].value is False


def test_a_failed_service_is_not_a_restart_loop() -> None:
    facts = vast_host("systemctl_show_failed.txt")
    assert facts["vast_host.active_state"].value == "failed"
    assert facts["vast_host.service_active"].value is False
    assert facts["vast_host.restart_loop"].value is False


def test_a_missing_unit_is_not_the_same_as_a_stopped_one() -> None:
    """Real systemd says LoadState=not-found for a unit that does not exist, and exits 0,
    which is why the collector uses `show` and not `is-active`."""
    facts = vast_host("systemctl_show_notfound.txt")
    assert facts["vast_host.unit_loaded"].value is False
    assert "not-found" in (facts["vast_host.unit_loaded"].raw_excerpt or "")
    assert facts["vast_host.service_active"].value is False


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("systemctl_show_vastai_active.txt", "pass"),
        ("systemctl_show_vastai_inactive.txt", "fail"),
        ("systemctl_show_vastai_autorestart.txt", "fail"),
        ("systemctl_show_failed.txt", "fail"),
        ("systemctl_show_notfound.txt", "fail"),
    ],
)
def test_the_service_rule_on_real_output(name: str, expected: str) -> None:
    facts = list(vast_host(name).values())
    (result,) = evaluate_rules([RULES["vast_host.service_not_active"]], facts)
    assert result.status.value == expected


def test_real_is_active_output_for_systemd_resolved() -> None:
    first_line = text("systemctl_is_active_resolved.txt").splitlines()[0]
    assert first_line == "active"
    ctx = fake_context(
        results={
            "network.resolved_active": CommandResult(
                "network.resolved_active",
                CommandStatus.OK,
                returncode=0,
                stdout=first_line + "\n",
            )
        },
        network=FakeNetwork(),
    )
    facts = {f.key: f for f in NetworkCollector().collect(ctx)}
    assert facts["network.resolved_active"].value is True


def test_real_resolvectl_status_has_the_shape_the_dns_fix_relies_on() -> None:
    """Not parsed by the tool (it reads resolv.conf and `is-active`), but this documents what
    a real resolved looks like: foreign resolv.conf mode, no link DNS until a fix sets one."""
    status = text("resolvectl_status.txt")
    assert "resolv.conf mode: foreign" in status
    assert "Link 2 (eth0)" in status and "-DefaultRoute" in status
