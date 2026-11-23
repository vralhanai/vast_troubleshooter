import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vast_troubleshooter import cli
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.diagnose import (
    EXIT_CRITICAL,
    EXIT_INCOMPLETE,
    EXIT_OK,
    elevated_commands,
    run_diagnosis,
)
from vast_troubleshooter.models import CheckStatus, Fact, FactStatus
from vast_troubleshooter.rule_engine import RuleFileError, load_default_rules
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeNetwork, fake_context

FIX = Path(__file__).parent / "fixtures"
RULES = load_default_rules()
runner = CliRunner()


def txt(*parts: str) -> str:
    return FIX.joinpath(*parts).read_text()


def ok(op: str, body: str) -> CommandResult:
    return CommandResult(op, CommandStatus.OK, returncode=0, stdout=body)


def healthy_results() -> dict[str, CommandResult]:
    """A healthy host: 8 GPUs, Docker with NVIDIA, a running daemon, a clean log, good disks."""
    return {
        "system.uname_release": ok("system.uname_release", "6.8.0-45-generic\n"),
        "gpu.query": ok("gpu.query", txt("gpu", "healthy_8gpu", "query.csv")),
        "gpu.throttle_event": ok(
            "gpu.throttle_event", txt("gpu", "healthy_8gpu", "throttle.csv")
        ),
        "gpu.lspci_nvidia": ok(
            "gpu.lspci_nvidia", txt("gpu", "healthy_8gpu", "lspci.txt")
        ),
        "driver.smi_summary": ok(
            "driver.smi_summary", txt("gpu", "healthy_8gpu", "smi.txt")
        ),
        "docker.info": ok("docker.info", txt("docker", "running_nvidia.json")),
        "kernel.journal": ok("kernel.journal", ""),
        "vast.service_show": ok(
            "vast.service_show", txt("vast_host", "show_active.txt")
        ),
        "vast.daemon_log_tail": ok(
            "vast.daemon_log_tail", txt("vast_host", "kaalia_clean.log")
        ),
        "disk.df[path=/]": ok("disk.df", txt("disk", "df_root.txt")),
        "disk.df[path=/var/lib/docker]": ok(
            "disk.df", txt("disk", "df_docker_own.txt")
        ),
        "disk.findmnt_root": ok("disk.findmnt_root", txt("disk", "findmnt_rw.txt")),
        "disk.lsblk": ok("disk.lsblk", txt("disk", "lsblk_nvme.txt")),
        "disk.smart_x[device=/dev/nvme0n1]": ok(
            "disk.smart_x", txt("disk", "smart_nvme_healthy.txt")
        ),
    }


def host(
    results: dict[str, CommandResult], network: FakeNetwork | None = None
) -> HostContext:
    return fake_context(
        results=results,
        files={
            "/etc/os-release": 'ID=ubuntu\nVERSION_ID="24.04"\n',
            "/proc/uptime": "100.0 200.0\n",
            "/etc/resolv.conf": "nameserver 127.0.0.53\n",
        },
        network=network or FakeNetwork(),
    )


def diagnose(ctx: HostContext):
    return run_diagnosis(ctx, RULES)


# -- the engine end to end ------------------------------------------------------------


def test_healthy_host_has_no_findings_and_exits_zero() -> None:
    d = diagnose(host(healthy_results()))
    assert d.findings == []
    assert d.unknown == []
    assert d.exit_code == EXIT_OK
    assert d.counts()["skipped"] == 1  # the opt-in outbound rule
    assert d.counts()["pass"] == len(RULES) - 1


def test_broken_dns_and_stopped_service_are_found_and_exit_one() -> None:
    results = healthy_results()
    results["vast.service_show"] = ok(
        "vast.service_show", txt("vast_host", "show_inactive.txt")
    )
    d = diagnose(host(results, FakeNetwork(socket.gaierror(-2, "no"))))
    assert [f.rule_id for f in d.findings] == [
        "network.dns_vast_ai_unresolvable",
        "vast_host.service_not_active",
    ]
    assert d.exit_code == EXIT_CRITICAL


def test_critical_findings_win_over_an_incomplete_diagnosis() -> None:
    results = healthy_results()
    results["kernel.journal"] = CommandResult(
        "kernel.journal", CommandStatus.NEEDS_ROOT
    )
    d = diagnose(host(results, FakeNetwork(socket.gaierror(-2, "no"))))
    assert d.incomplete
    assert d.exit_code == EXIT_CRITICAL


def test_unreadable_critical_check_is_incomplete_not_ok() -> None:
    results = healthy_results()
    results["kernel.journal"] = CommandResult(
        "kernel.journal", CommandStatus.NEEDS_ROOT
    )
    d = diagnose(host(results))
    assert d.findings == []
    assert d.incomplete
    assert d.exit_code == EXIT_INCOMPLETE
    assert d.needs_root_hint
    assert "kernel_log.xid_79_gpu_fell_off_bus" in [r.rule_id for r in d.unknown]


def test_an_unknown_warning_alone_is_not_incomplete() -> None:
    results = healthy_results()
    results["disk.smart_x[device=/dev/nvme0n1]"] = ok(
        "disk.smart_x", txt("disk", "smart_nvme_healthy.txt")
    )
    d = diagnose(host(results))
    # Make only the warning-level SATA rule unknown by dropping its facts.
    facts = [f for f in d.facts if "sata_" not in f.key]
    from vast_troubleshooter.diagnose import Diagnosis
    from vast_troubleshooter.rule_engine import evaluate_rules

    partial = Diagnosis(tuple(facts), tuple(evaluate_rules(RULES, facts)))
    statuses = {r.rule_id: r.status for r in partial.results}
    assert statuses["disk.sata_bad_sectors"] is CheckStatus.UNKNOWN
    assert not partial.incomplete
    assert partial.exit_code == EXIT_OK


def test_a_crashing_collector_makes_its_checks_unknown_not_ok() -> None:
    ctx = host(healthy_results())
    bad = HostContext(
        runner=ctx.runner, files=ctx.files
    )  # no network: network collector crashes
    d = diagnose(bad)
    keys = {f.key for f in d.facts}
    assert "network.collector" in keys
    assert any(
        r.rule_id == "network.dns_vast_ai_unresolvable"
        and r.status is CheckStatus.UNKNOWN
        for r in d.results
    )
    assert d.exit_code == EXIT_INCOMPLETE


def test_diagnosis_does_not_change_the_system() -> None:
    ctx = host(healthy_results())
    diagnose(ctx)
    for op_id, _ in ctx.runner.calls:  # type: ignore[attr-defined]
        assert not op_id.startswith(("fix.", "write.", "restart."))


def test_elevated_commands_lists_only_fixed_read_commands() -> None:
    commands = elevated_commands()
    assert any(c.startswith("journalctl -k -b") for c in commands)
    assert any(c.startswith("smartctl -x") for c in commands)
    assert not any(" -t " in f" {c} " for c in commands)


# -- the command ------------------------------------------------------------------------


def run_cli(
    monkeypatch: pytest.MonkeyPatch,
    ctx: HostContext,
    *args: str,
    seen: list | None = None,
):
    def make(allow_outbound: bool = False, allow_elevated: bool = False) -> HostContext:
        if seen is not None:
            seen.append((allow_outbound, allow_elevated))
        return ctx

    monkeypatch.setattr(cli, "default_context", make)
    return runner.invoke(cli.app, ["diagnose", *args])


def test_human_output_for_a_healthy_host(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, host(healthy_results()))
    assert result.exit_code == 0
    assert "No problems found." in result.stdout
    assert (
        "could not be checked" in result.stdout
    )  # the "0 could not be checked" summary
    assert "Could not be checked (" not in result.stdout


def test_human_output_shows_summary_evidence_and_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = host(healthy_results(), FakeNetwork(socket.gaierror(-2, "no")))
    result = run_cli(monkeypatch, ctx)
    assert result.exit_code == 1
    out = result.stdout
    assert "CRITICAL" in out
    assert "DNS cannot resolve vast.ai" in out
    assert "evidence: network.dns_vast_ai_resolves = False [ok]" in out
    assert "what to do:" in out and "resolver" in out


def test_findings_are_printed_most_severe_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = healthy_results()
    results["kernel.journal"] = ok(
        "kernel.journal",
        "2026-11-23T10:00:00+0000 h kernel: NVRM: Xid (PCI:0000:3a:00): 79, x\n",
    )
    out = run_cli(monkeypatch, host(results)).stdout
    assert out.index("CRITICAL") < out.index("WARNING")


def test_incomplete_run_says_so_and_hints_at_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = healthy_results()
    results["kernel.journal"] = CommandResult(
        "kernel.journal", CommandStatus.NEEDS_ROOT
    )
    result = run_cli(monkeypatch, host(results))
    assert result.exit_code == EXIT_INCOMPLETE
    assert "Could not be checked" in result.stdout
    assert "kernel_log.xid_count: needs_root" in result.stdout
    assert "--allow-root-reads" in result.stdout
    assert "diagnosis is incomplete" in result.stdout
    assert "No problems found in the checks that ran." in result.stdout


def test_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    results = healthy_results()
    results["kernel.journal"] = CommandResult(
        "kernel.journal", CommandStatus.NEEDS_ROOT
    )
    result = run_cli(
        monkeypatch, host(results, FakeNetwork(socket.gaierror(-2, "no"))), "--json"
    )
    data = json.loads(result.stdout)
    assert result.exit_code == data["exit_code"] == 1
    assert data["schema_version"] == 1
    assert data["complete"] is False
    assert data["findings"][0]["rule_id"] == "network.dns_vast_ai_unresolvable"
    assert data["unknown"][0]["reasons"]
    assert {"collector", "key", "status", "timestamp"} <= set(data["facts"][0])
    assert data["counts"]["fail"] == 1


def test_json_facts_never_carry_raw_excerpts_or_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = healthy_results()
    results["docker.info"] = CommandResult(
        "docker.info",
        CommandStatus.ERROR,
        returncode=1,
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )
    out = run_cli(monkeypatch, host(results), "--json").stdout
    assert "raw_excerpt" not in out and "source_command" not in out
    assert "Cannot connect" not in out
    assert "/var/run/docker.sock" not in out


def test_json_stdout_is_pure_json_even_with_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = host(healthy_results())
    result = run_cli(
        monkeypatch, ctx, "--json", "--allow-root-reads", "--allow-outbound"
    )
    json.loads(result.stdout)  # notices went to stderr, not stdout


def test_defaults_are_local_only_and_unelevated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []
    run_cli(monkeypatch, host(healthy_results()), seen=seen)
    assert seen == [(False, False)]


def test_flags_are_passed_through_and_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []
    result = run_cli(
        monkeypatch,
        host(healthy_results()),
        "--allow-root-reads",
        "--allow-outbound",
        seen=seen,
    )
    assert seen == [(True, True)]
    assert "Elevated reads are enabled" in result.output
    assert "journalctl -k -b" in result.output
    assert "1.1.1.1:443" in result.output


def test_no_announcement_without_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, host(healthy_results()))
    assert "Elevated reads are enabled" not in result.output
    assert "Outbound probes are enabled" not in result.output


def test_a_broken_rule_file_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list:
        raise RuleFileError("bad.toml: broken")

    monkeypatch.setattr(cli, "load_default_rules", boom)
    result = runner.invoke(cli.app, ["diagnose"])
    assert result.exit_code == 2
    assert "bad.toml" in result.output


def test_an_unexpected_failure_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_diagnosis", boom)
    monkeypatch.setattr(cli, "default_context", lambda **kw: host({}))
    result = runner.invoke(cli.app, ["diagnose"])
    assert result.exit_code == 2
    assert "RuntimeError: kaboom" in result.output


def test_evidence_line_neutralizes_markup_and_control_characters() -> None:
    fact = Fact(
        collector="x", key="x.k", value="[red]hi[/red]\x1b[31m", status=FactStatus.OK
    )
    line = cli.evidence_line("x.k", {"x.k": fact})
    assert "x.k = " in line
    from rich.markup import escape

    shown = escape(cli.safe_text(line))
    assert "\x1b" not in shown
    assert "\\[red]" in shown  # markup is escaped, so it prints literally


def test_help_lists_diagnose() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert "diagnose" in result.stdout
