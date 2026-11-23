import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vast_troubleshooter import cli
from vast_troubleshooter.redact import Redactor
from vast_troubleshooter.report import Report
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeNetwork, fake_context

runner = CliRunner()
HF = "hf_" + "x" * 34


def ok(op: str, text: str) -> CommandResult:
    return CommandResult(op, CommandStatus.OK, returncode=0, stdout=text)


def setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    dns_ok: bool = False,
    uname: str = "6.8.0-45-generic",
) -> Path:
    """A fake host whose kernel string carries identifying text, with a state dir in tmp."""
    state = tmp_path / "state"
    ctx = fake_context(
        results={"system.uname_release": ok("system.uname_release", uname + "\n")},
        files={"/etc/resolv.conf": "nameserver 127.0.0.53\n"},
        network=FakeNetwork(["203.0.113.7"] if dns_ok else socket.gaierror(-2, "no")),
    )
    monkeypatch.setattr(cli, "default_context", lambda **kw: ctx)
    monkeypatch.setattr(cli, "state_dir", lambda: state)
    monkeypatch.setattr(
        cli, "default_redactor", lambda: Redactor(["secrethost"], ["secretuser"], [])
    )
    return state


def invoke(*args: str):
    return runner.invoke(cli.app, ["report", *args])


def written(directory: Path) -> tuple[Path, Path]:
    files = sorted(directory.iterdir())
    assert len(files) == 2, files
    return files[0], files[1]  # .json then .md


def test_it_writes_a_json_and_a_markdown_file_and_prints_their_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    out = tmp_path / "out"
    result = invoke("--output-dir", str(out))
    assert result.exit_code == 0
    json_path, md_path = written(out)
    assert json_path.suffix == ".json" and md_path.suffix == ".md"
    assert str(json_path) in result.stdout and str(md_path) in result.stdout
    assert "Nothing was uploaded" in result.stdout
    assert "Read both files before you share them" in result.stdout
    assert "bytes" in result.stdout


def test_the_json_file_is_a_valid_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    invoke("--output-dir", str(tmp_path / "out"))
    json_path, _ = written(tmp_path / "out")
    report = Report.model_validate_json(json_path.read_text())
    assert (
        report.findings
        and report.findings[0].rule_id == "network.dns_vast_ai_unresolvable"
    )
    assert report.schema_version == 1


def test_a_report_is_written_even_when_problems_are_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path, dns_ok=False)
    result = invoke("--output-dir", str(tmp_path / "out"))
    assert (
        result.exit_code == 0
    )  # the report was made; findings do not make the command fail
    assert "1 problem(s)" in result.stdout


def test_identifying_text_is_redacted_in_both_files_and_the_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path, uname=f"6.8.0 secrethost secretuser 10.9.8.7 {HF}")
    out = tmp_path / "out"
    result = invoke("--output-dir", str(out), "--show")
    json_path, md_path = written(out)
    for text in (json_path.read_text(), md_path.read_text(), result.output):
        for leaked in ("secrethost", "secretuser", "10.9.8.7", HF):
            assert leaked not in text


def test_show_prints_the_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    shown = invoke("--output-dir", str(tmp_path / "out"), "--show")
    quiet = invoke("--output-dir", str(tmp_path / "out2"))
    assert "# Vast Troubleshooter report" in shown.stdout
    assert "# Vast Troubleshooter report" not in quiet.stdout


def test_the_default_folder_is_private_and_inside_the_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = setup(monkeypatch, tmp_path)
    assert invoke().exit_code == 0
    assert len(list((state / "reports").iterdir())) == 2


def test_fix_actions_from_the_log_are_included_without_extra_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = setup(monkeypatch, tmp_path)
    state.mkdir()
    entry = {"run_id": "r1", "fix_id": "restart-vast-daemon", "phase": "blocked", "status": "blocked",
             "block_code": "rental_unknown", "reasons": ["rental state is unknown"], "stdout": "SECRET-OUTPUT"}  # fmt: skip
    (state / "actions.jsonl").write_text(json.dumps(entry) + "\n")
    invoke("--output-dir", str(tmp_path / "out"))
    json_path, md_path = written(tmp_path / "out")
    report = Report.model_validate_json(json_path.read_text())
    assert [a.run_id for a in report.actions] == ["r1"]
    assert "SECRET-OUTPUT" not in json_path.read_text() + md_path.read_text()
    assert "rental_unknown" in json_path.read_text()


def test_two_reports_never_overwrite_each_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    out = tmp_path / "out"
    invoke("--output-dir", str(out))
    invoke("--output-dir", str(out))
    assert len(list(out.iterdir())) == 4


@pytest.mark.parametrize(
    "flag", ["--no-redact", "--raw", "--keep-ips", "--unredacted", "--no-redaction"]
)
def test_there_is_no_option_to_keep_identifying_text(flag: str) -> None:
    result = runner.invoke(cli.app, ["report", flag])
    assert result.exit_code == 2  # unknown option
    assert "report" in runner.invoke(cli.app, ["--help"]).stdout
    assert "redact" not in runner.invoke(
        cli.app, ["report", "--help"]
    ).stdout.lower().replace("redacted report", "").replace("redacted", "")


def test_it_never_uploads_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    net = FakeNetwork()
    ctx = fake_context(network=net)
    monkeypatch.setattr(cli, "default_context", lambda **kw: ctx)
    invoke("--output-dir", str(tmp_path / "out"))
    assert (
        net.connect_calls == []
    )  # no outbound probe, and the report code has no network at all
    import vast_troubleshooter.report as report_module

    source = Path(report_module.__file__).read_text()
    for forbidden in ("import socket", "urllib", "requests", "http.client", "smtplib"):
        assert forbidden not in source


def test_an_unusable_output_directory_is_a_tool_error_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    result = invoke("--output-dir", str(blocker))
    assert result.exit_code == 2
    assert "error:" in result.output and "output directory" in result.output
    assert blocker.read_text() == "x"


def test_a_failed_redaction_check_is_a_tool_error_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path, uname="6.8.0 token=hunter2")

    class Leaky(Redactor):
        def obj(self, data):  # simulates a redaction bug
            return data

    monkeypatch.setattr(cli, "default_redactor", lambda: Leaky())
    out = tmp_path / "out"
    result = invoke("--output-dir", str(out))
    assert result.exit_code == 2
    assert "redaction check failed" in result.output
    assert not out.exists() or list(out.iterdir()) == []


def test_flags_are_passed_through_like_diagnose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    seen: list[tuple[bool, bool]] = []
    ctx = fake_context()
    monkeypatch.setattr(
        cli,
        "default_context",
        lambda allow_outbound=False, allow_elevated=False: (
            seen.append((allow_outbound, allow_elevated)),
            ctx,
        )[1],
    )
    invoke("--output-dir", str(tmp_path / "o1"))
    invoke(
        "--output-dir", str(tmp_path / "o2"), "--allow-root-reads", "--allow-outbound"
    )
    assert seen == [(False, False), (True, True)]


def test_diagnose_still_works_after_sharing_its_collection_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["diagnose", "--json"])
    assert json.loads(result.stdout)["schema_version"] == 1
