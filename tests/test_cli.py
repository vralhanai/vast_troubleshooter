import pytest
from typer.testing import CliRunner

from vast_troubleshooter import __version__, cli
from vast_troubleshooter.cli import app
from vast_troubleshooter.models import Fact
from vast_troubleshooter.testing import fake_context

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_lists_version_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout
    assert "collect" in result.stdout


def test_collect_system_prints_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = fake_context(
        results={"system.uname_release": "6.8.0-45-generic\n"},
        files={
            "/etc/os-release": 'ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04"\n',
            "/proc/uptime": "3600.25 7000.5\n",
        },
    )
    monkeypatch.setattr(cli, "default_context", lambda allow_outbound=False: ctx)
    result = runner.invoke(app, ["collect", "system"])
    assert result.exit_code == 0
    assert "system.os_version_id = 24.04 [ok]" in result.stdout
    assert "system.kernel_release = 6.8.0-45-generic [ok]" in result.stdout
    assert "system.uptime_seconds = 3600.25 s [ok]" in result.stdout


def test_collect_unknown_collector_exits_with_error() -> None:
    result = runner.invoke(app, ["collect", "nope"])
    assert result.exit_code == 2
    assert "unknown collector" in result.output


def test_output_neutralizes_terminal_control_characters() -> None:
    nasty = Fact(
        collector="system",
        key="system.x",
        value="ok\x1b[31mRED\x07",
        raw_excerpt="line1\nline2\x1b]0;title\x07",
    )
    line = cli.format_fact(nasty)
    assert "\x1b" not in line
    assert "\x07" not in line
    assert "\n" not in line
