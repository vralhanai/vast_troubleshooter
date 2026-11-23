from pathlib import Path

import pytest

from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.vast_host import (
    LOG_KEYS,
    SERVICE_KEYS,
    VastHostCollector,
    analyze_log,
    parse_show,
)
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import CommandResult, CommandStatus, SubprocessRunner
from vast_troubleshooter.testing import fake_context

FIXTURES = Path(__file__).parent / "fixtures" / "vast_host"
SHOW = "vast.service_show"
TAIL = "vast.daemon_log_tail"


def text(name: str) -> str:
    return (FIXTURES / name).read_text()


def ok(op: str, body: str) -> CommandResult:
    return CommandResult(op, CommandStatus.OK, returncode=0, stdout=body)


def failed(
    op: str,
    status: CommandStatus = CommandStatus.ERROR,
    stdout: str = "",
    stderr: str = "",
    detail: str = "",
) -> CommandResult:
    return CommandResult(
        op, status, returncode=1, stdout=stdout, stderr=stderr, detail=detail
    )


def collect(show: CommandResult, tail: CommandResult | None = None) -> dict[str, Fact]:
    ctx = fake_context(
        results={SHOW: show, TAIL: tail or ok(TAIL, text("kaalia_clean.log"))}
    )
    return {f.key: f for f in VastHostCollector().collect(ctx)}


def service(name: str) -> dict[str, Fact]:
    return collect(ok(SHOW, text(name)))


# -- service state ----------------------------------------------------------------


def test_active_service() -> None:
    facts = service("show_active.txt")
    assert facts["vast_host.unit_loaded"].value is True
    assert facts["vast_host.service_active"].value is True
    assert facts["vast_host.active_state"].value == "active"
    assert facts["vast_host.sub_state"].value == "running"
    assert facts["vast_host.nrestarts"].value == 0
    assert facts["vast_host.restart_loop"].value is False


def test_inactive_service() -> None:
    facts = service("show_inactive.txt")
    assert facts["vast_host.unit_loaded"].value is True
    assert facts["vast_host.service_active"].value is False
    assert facts["vast_host.active_state"].value == "inactive"
    assert facts["vast_host.restart_loop"].value is False


def test_failed_service() -> None:
    facts = service("show_failed.txt")
    assert facts["vast_host.service_active"].value is False
    assert facts["vast_host.active_state"].value == "failed"
    assert facts["vast_host.nrestarts"].value == 5
    assert facts["vast_host.restart_loop"].value is False  # failed, not retrying


def test_restart_loop_is_detected() -> None:
    facts = service("show_restart_loop.txt")
    assert facts["vast_host.restart_loop"].value is True
    assert facts["vast_host.service_active"].value is False
    assert facts["vast_host.sub_state"].value == "auto-restart"
    assert facts["vast_host.nrestarts"].value == 12


def test_missing_unit_is_not_the_same_as_a_stopped_unit() -> None:
    missing = service("show_not_found.txt")
    stopped = service("show_inactive.txt")
    assert missing["vast_host.unit_loaded"].value is False
    assert missing["vast_host.unit_loaded"].status is FactStatus.OK  # an observation
    assert "not-found" in (missing["vast_host.unit_loaded"].raw_excerpt or "")
    assert stopped["vast_host.unit_loaded"].value is True
    assert missing["vast_host.service_active"].value is False


def test_systemctl_not_installed() -> None:
    facts = collect(failed(SHOW, CommandStatus.MISSING, detail="systemctl not found"))
    assert all(facts[k].status is FactStatus.MISSING for k in SERVICE_KEYS)


def test_host_without_systemd_is_unsupported() -> None:
    facts = collect(failed(SHOW, stderr=text("no_systemd.txt")))
    for key in SERVICE_KEYS:
        assert facts[key].status is FactStatus.UNSUPPORTED
        assert facts[key].value is None


def test_systemctl_hang_is_a_timeout() -> None:
    facts = collect(failed(SHOW, CommandStatus.TIMEOUT, detail="timed out after 10.0s"))
    assert all(facts[k].status is FactStatus.TIMEOUT for k in SERVICE_KEYS)


def test_unparseable_systemctl_output_is_an_error() -> None:
    facts = collect(ok(SHOW, "this is not key value output"))
    assert all(facts[k].status is FactStatus.ERROR for k in SERVICE_KEYS)


def test_missing_properties_are_missing_facts() -> None:
    facts = collect(ok(SHOW, "ActiveState=active\n"))
    assert facts["vast_host.service_active"].value is True
    assert facts["vast_host.unit_loaded"].status is FactStatus.MISSING
    assert facts["vast_host.sub_state"].status is FactStatus.MISSING
    assert facts["vast_host.nrestarts"].status is FactStatus.MISSING
    assert facts["vast_host.restart_loop"].status is FactStatus.MISSING


def test_non_numeric_restart_count_only_affects_that_fact() -> None:
    facts = collect(
        ok(
            SHOW,
            "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=abc\n",
        )
    )
    assert facts["vast_host.nrestarts"].status is FactStatus.ERROR
    assert facts["vast_host.service_active"].value is True


# -- log --------------------------------------------------------------------------


def test_clean_log() -> None:
    facts = service("show_active.txt")
    assert facts["vast_host.log_lines_read"].value == 3
    assert facts["vast_host.log_error_lines"].value == 0
    assert "vast_host.log_last_error_time" not in facts


def test_error_log_counts_and_categories() -> None:
    facts = collect(
        ok(SHOW, text("show_active.txt")), ok(TAIL, text("kaalia_errors.log"))
    )
    assert facts["vast_host.log_lines_read"].value == 8
    # ERROR x3 plus the "Traceback" line; WARNING and INFO lines are not errors.
    assert facts["vast_host.log_error_lines"].value == 4
    assert (
        facts["vast_host.log_error_network_lines"].value == 2
    )  # timed out; Docker connect
    assert facts["vast_host.log_error_docker_lines"].value == 1
    assert facts["vast_host.log_error_gpu_lines"].value == 1
    assert facts["vast_host.log_last_error_time"].value == "2026-11-23T10:00:09"


def test_empty_log_is_zero_lines() -> None:
    facts = collect(ok(SHOW, text("show_active.txt")), ok(TAIL, ""))
    assert facts["vast_host.log_lines_read"].value == 0
    assert facts["vast_host.log_error_lines"].value == 0


def test_missing_log_file_is_separate_from_a_missing_service() -> None:
    tail = failed(
        TAIL,
        stderr="tail: cannot open '/var/lib/vastai_kaalia/kaalia.log' for reading: No such file or directory",
    )
    facts = collect(ok(SHOW, text("show_active.txt")), tail)
    assert all(facts[k].status is FactStatus.MISSING for k in LOG_KEYS)
    assert "kaalia.log not found" in (
        facts["vast_host.log_lines_read"].raw_excerpt or ""
    )
    assert facts["vast_host.service_active"].value is True  # service facts unaffected


def test_service_missing_and_log_present_are_reported_independently() -> None:
    facts = collect(
        ok(SHOW, text("show_not_found.txt")), ok(TAIL, text("kaalia_clean.log"))
    )
    assert facts["vast_host.unit_loaded"].value is False
    assert facts["vast_host.log_lines_read"].status is FactStatus.OK


def test_log_needs_root_without_hiding_the_service_state() -> None:
    tail = CommandResult(
        TAIL,
        CommandStatus.NEEDS_ROOT,
        detail="needs root; elevated reads are not enabled",
    )
    facts = collect(ok(SHOW, text("show_active.txt")), tail)
    for key in LOG_KEYS:
        assert facts[key].status is FactStatus.NEEDS_ROOT
        assert facts[key].value is None
    assert facts["vast_host.service_active"].value is True


def test_unreadable_log_never_looks_clean() -> None:
    tail = CommandResult(TAIL, CommandStatus.NEEDS_ROOT)
    facts = collect(ok(SHOW, text("show_active.txt")), tail)
    assert not any(facts[k].value == 0 for k in LOG_KEYS)


@pytest.mark.parametrize("status", [CommandStatus.TIMEOUT, CommandStatus.MISSING])
def test_log_command_failures(status: CommandStatus) -> None:
    facts = collect(ok(SHOW, text("show_active.txt")), failed(TAIL, status))
    expected = (
        FactStatus.TIMEOUT if status is CommandStatus.TIMEOUT else FactStatus.MISSING
    )
    assert all(facts[k].status is expected for k in LOG_KEYS)


def test_other_tail_failure_is_an_error() -> None:
    facts = collect(
        ok(SHOW, text("show_active.txt")), failed(TAIL, stderr="tail: weird failure")
    )
    assert all(facts[k].status is FactStatus.ERROR for k in LOG_KEYS)


# -- nothing from the log reaches a fact ------------------------------------------


def test_no_log_text_reaches_any_fact() -> None:
    facts = collect(
        ok(SHOW, text("show_active.txt")), ok(TAIL, text("kaalia_errors.log"))
    )
    dumped = " ".join(f.model_dump_json() for f in facts.values())
    for leaked in (
        "99999",
        "203.0.113.9",
        "cust-abc123",
        "424242",
        "main.py",
        "Traceback",
        "controller",
    ):
        assert leaked not in dumped


def test_facts_are_safe_to_share() -> None:
    facts = collect(
        ok(SHOW, text("show_restart_loop.txt")), ok(TAIL, text("kaalia_errors.log"))
    )
    for fact in facts.values():
        fact.to_shareable()


# -- parsers ----------------------------------------------------------------------


def test_parse_show() -> None:
    assert parse_show("A=1\nB=two words\nnonsense\n=x\nC=\n") == {
        "A": "1",
        "B": "two words",
        "C": "",
    }


@pytest.mark.parametrize(
    ("line", "stamp"),
    [
        ("2026-11-23 10:00:05,200 ERROR x", "2026-11-23T10:00:05"),
        ("2026-11-23T10:00:05.200Z ERROR x", "2026-11-23T10:00:05"),
        ("[2026-11-23 10:00:05] ERROR x", "2026-11-23T10:00:05"),
        ("ERROR no timestamp here", None),
    ],
)
def test_log_time_formats(line: str, stamp: str | None) -> None:
    found = analyze_log(line)
    assert found.get("last_error") == stamp


def test_error_words_must_be_whole_words() -> None:
    assert analyze_log("terrorist errorless\n")["errors"] == 0


# -- operations and the elevated-read policy ---------------------------------------


def test_log_operation_is_a_fixed_elevated_read() -> None:
    op = READ_OPERATIONS[TAIL]
    assert op.needs_root
    assert op.args == ("-n", "100", "/var/lib/vastai_kaalia/kaalia.log")
    assert not READ_OPERATIONS[SHOW].needs_root


def test_without_elevation_the_log_is_not_read_but_the_service_is_asked() -> None:
    runner = SubprocessRunner(
        READ_OPERATIONS,
        is_root=lambda: False,
        which=lambda name, path: f"/usr/bin/{name}",
    )
    assert runner.plan(TAIL)[1] is not None
    assert runner.plan(TAIL)[1].status is CommandStatus.NEEDS_ROOT  # type: ignore[union-attr]
    argv, early = runner.plan(SHOW)
    assert early is None
    assert argv[:3] == ["/usr/bin/systemctl", "show", "vastai.service"]


def test_registry_includes_vast_host() -> None:
    assert "vast_host" in COLLECTORS
