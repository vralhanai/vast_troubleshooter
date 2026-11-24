import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vast_troubleshooter import report as report_module
from vast_troubleshooter.diagnose import Diagnosis
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.redact import Redactor
from vast_troubleshooter.report import (
    Report,
    ReportError,
    build_report,
    make_report_files,
    md,
    read_actions,
    render_markdown,
    write_report,
)
from vast_troubleshooter.rule_engine import evaluate_rules, load_default_rules
from vast_troubleshooter.securefs import create_private_file, ensure_private_dir

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
RULES = load_default_rules()
R = Redactor(
    hostnames=["gpu-rig-07"],
    usernames=["alice"],
    machine_ids=["0123456789abcdef0123456789abcdef"],
)
HF = "hf_" + "x" * 34
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX permissions and symlinks"
)


def F(
    key: str, value: object = None, status: FactStatus = FactStatus.OK, **kw: object
) -> Fact:
    return Fact(collector=key.split(".")[0], key=key, value=value, status=status, **kw)  # type: ignore[arg-type]


def diagnosis(*facts: Fact) -> Diagnosis:
    return Diagnosis(tuple(facts), tuple(evaluate_rules(RULES, facts)))


def broken_host() -> Diagnosis:
    return diagnosis(
        F("system.os_pretty_name", "Ubuntu 24.04.1 LTS"),
        F("system.kernel_release", "6.8.0-45-generic"),
        F("system.uptime_seconds", 3600.5, unit="s"),
        F("gpu.count", 8),
        F("gpu.0.name", "NVIDIA H100 80GB HBM3"),
        F("driver.version", "580.178.04"),
        F("driver.cuda_version", "13.0"),
        F("docker.server_version", "27.3.1"),
        F(
            "network.dns_vast_ai_resolves",
            False,
            source_command="getaddrinfo vast.ai",
            raw_excerpt="resolution failed",
        ),
        F("rentals.state", "unknown"),
        F("rentals.blocks_disruptive_fixes", True),
        F("rentals.reason", "VM rental detection is not implemented"),
        F("rentals.unresolved_sources", "vms"),
        F(
            "kernel_log.xid_count",
            None,
            FactStatus.NEEDS_ROOT,
            raw_excerpt="needs root",
        ),
    )


def make(diag: Diagnosis | None = None, actions: list | None = None) -> tuple[str, str]:
    return make_report_files(diag or broken_host(), R, actions, NOW)


# -- contents and schema ---------------------------------------------------------------------


def test_the_json_validates_against_the_report_model() -> None:
    json_text, _ = make()
    report = Report.model_validate_json(json_text)
    assert report.schema_version == 1
    assert report.tool_version
    assert report.redaction.applied and report.redaction.version == 1
    assert report.generated_at == "2026-11-23T12:00:00+00:00"


def test_the_report_has_the_planned_sections() -> None:
    report = Report.model_validate_json(make()[0])
    assert report.summary.exit_code == 1 and report.summary.complete is False
    assert report.summary.counts["fail"] == 1
    assert [f.rule_id for f in report.findings] == ["network.dns_vast_ai_unresolvable"]
    finding = report.findings[0]
    assert finding.severity == "critical" and finding.fix_id == "fix-dns-resolver"
    assert (finding.evidence[0].key, finding.evidence[0].value, finding.evidence[0].status) == (
        "network.dns_vast_ai_resolves", False, "ok",
    )  # fmt: skip
    assert report.unknown and all(u.reasons for u in report.unknown)
    assert report.host.os == "Ubuntu 24.04.1 LTS" and report.host.gpu_count == 8
    assert (
        report.host.driver_version == "580.178.04"
        and report.host.uptime_seconds == 3600.5
    )
    assert (
        report.rental.state == "unknown"
        and report.rental.blocks_disruptive_fixes is True
    )
    assert report.rental.unresolved_sources == "vms"
    assert {f.key for f in report.facts} >= {
        "gpu.count",
        "network.dns_vast_ai_resolves",
    }


def test_a_host_with_no_data_still_gives_a_valid_report() -> None:
    report = Report.model_validate_json(make(diagnosis())[0])
    assert report.host.os is None and report.rental.state == "unknown"
    assert report.findings == []


def test_the_report_model_rejects_unknown_fields() -> None:
    data = json.loads(make()[0])
    data["raw_log"] = "x"
    with pytest.raises(ValueError):
        Report.model_validate(data)


# -- the allowlist: raw data has no place to go -------------------------------------------------


def test_raw_excerpts_and_commands_are_never_in_the_report() -> None:
    diag = diagnosis(
        F(
            "docker.server_version",
            "27.3.1",
            source_command="docker --host unix:///x info",
            raw_excerpt="SECRET-EXCERPT",
        ),
        F(
            "kernel_log.xid_count",
            None,
            FactStatus.ERROR,
            raw_excerpt="NVRM: Xid (PCI:0000:3a:00): 79 pid=4242 gpu-rig-07",
        ),
    )
    json_text, markdown = make(diag)
    for text in (json_text, markdown):
        assert (
            "SECRET-EXCERPT" not in text
            and "NVRM" not in text
            and "docker --host" not in text
        )
    assert "raw_excerpt" not in json_text and "source_command" not in json_text


def test_a_fact_that_is_not_safe_to_share_is_left_out_not_guessed() -> None:
    diag = diagnosis(
        F("x.long", "a" * 500), F("x.ctrl", "line1\nline2"), F("x.fine", "ok")
    )
    keys = {f.key for f in Report.model_validate_json(make(diag)[0]).facts}
    assert keys == {"x.fine"}


# -- redaction as the second layer ---------------------------------------------------------------


def test_seeded_identifiers_in_values_are_redacted_everywhere() -> None:
    diag = diagnosis(
        F(
            "docker.server_version",
            f"27.3.1 10.9.8.7 gpu-rig-07 alice bob@example.org {HF}",
        ),
        F("rentals.reason", "container on 192.168.5.5 for user alice"),
        F("network.dns_vast_ai_resolves", False),
    )
    json_text, markdown = make(diag)
    for text in (json_text, markdown):
        for leaked in (
            "10.9.8.7",
            "192.168.5.5",
            "gpu-rig-07",
            "alice",
            "bob@example.org",
            HF,
        ):
            assert leaked not in text


def test_nothing_in_the_report_looks_sensitive_to_a_second_pass() -> None:
    json_text, _ = make()
    assert not R.leaves_something(json_text)


def test_the_guard_refuses_to_produce_a_report_if_redaction_missed_something() -> None:
    class Leaky(Redactor):
        def obj(self, data):  # simulates a redaction bug: values pass through unchanged
            return data

    diag = diagnosis(F("docker.server_version", "27.3.1 token=hunter2"))
    with pytest.raises(ReportError, match="redaction check failed"):
        make_report_files(diag, Leaky(), None, NOW)


# -- actions from the action log -------------------------------------------------------------------


def write_log(path: Path, lines: list[object]) -> Path:
    path.write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
    )
    return path


def test_actions_keep_only_allowlisted_keys_and_skip_bad_lines(tmp_path: Path) -> None:
    log = write_log(tmp_path / "a.jsonl", [
        {"time": "2026-11-23T10:00:00+00:00", "run_id": "r1", "fix_id": "restart-vast-daemon", "phase": "blocked",
         "status": "blocked", "block_code": "rental_unknown", "reasons": ["rental state is unknown"],
         "stdout": "SECRET-OUTPUT", "password": "hunter2", "schema_version": 1},
        "this is not json",
        "[1, 2, 3]",
        {"run_id": "r2", "reasons": "should be a list"},  # does not fit the schema
    ])  # fmt: skip
    entries = read_actions(log)
    assert entries[0].keys() <= set(report_module._ACTION_KEYS)
    assert "stdout" not in entries[0] and "password" not in entries[0]
    report = Report.model_validate_json(make(actions=entries)[0])
    assert [a.run_id for a in report.actions] == [
        "r1"
    ]  # the bad-schema line is dropped
    assert "SECRET-OUTPUT" not in make(actions=entries)[0]


def test_only_the_most_recent_actions_are_included(tmp_path: Path) -> None:
    log = write_log(
        tmp_path / "a.jsonl",
        [{"run_id": f"r{i}", "phase": "dry_run"} for i in range(200)],
    )
    entries = read_actions(log, limit=10)
    assert [e["run_id"] for e in entries] == [f"r{i}" for i in range(190, 200)]


def test_action_text_is_redacted(tmp_path: Path) -> None:
    log = write_log(
        tmp_path / "a.jsonl",
        [{"run_id": "r", "reasons": ["could not reach 10.1.2.3 as alice"]}],
    )
    out = make(actions=read_actions(log))[0]
    assert "10.1.2.3" not in out and "alice" not in out


def test_a_missing_or_symlinked_action_log_gives_nothing(tmp_path: Path) -> None:
    assert read_actions(None) == [] and read_actions(tmp_path / "nope.jsonl") == []
    assert read_actions(tmp_path) == []  # a directory


@posix_only
def test_a_symlinked_action_log_is_not_followed(tmp_path: Path) -> None:
    target = write_log(tmp_path / "real.jsonl", [{"run_id": "x"}])
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    assert read_actions(link) == []


def test_a_report_with_no_actions_says_so() -> None:
    assert "No fix has been run" in make()[1]


# -- markdown is inert text --------------------------------------------------------------------------------


NASTY = "[click](javascript:alert(1)) <script>x</script>\n# fake heading\x1b[31m **bold** `code` | cell http://evil.example/x &amp; ![img](u)"


def test_md_neutralizes_every_markdown_and_terminal_construct() -> None:
    out = md(NASTY)
    assert "\n" not in out and "\x1b" not in out
    assert not re.search(r"(?<!\\)[<>\[\]()*`|#!&]", out)
    assert "://" not in out


def test_host_text_cannot_format_the_markdown_report() -> None:
    diag = diagnosis(
        F("docker.server_version", NASTY[:190]),
        F("rentals.reason", NASTY[:190]),
        F("network.dns_vast_ai_resolves", False),
    )
    markdown = make(diag, [{"run_id": "r", "reasons": [NASTY], "phase": "blocked"}])[1]
    assert not re.search(r"(?<!\\)<", markdown)  # every < is escaped, so it is text
    assert "\x1b" not in markdown
    assert not re.search(r"(?<!\\)\]\(", markdown)  # no link syntax
    headings = [line for line in markdown.splitlines() if line.startswith("#")]
    assert all(
        h in {"# Vast Troubleshooter report", "## Summary", "## Host", "## Rental state", "## Findings",
              "## Could not be checked", "## Fix actions", "## Facts read"} or h.startswith("### CRITICAL")
        for h in headings
    )  # fmt: skip
    assert "fake heading" not in "\n".join(headings)


def test_the_markdown_lists_findings_with_evidence_and_advice() -> None:
    markdown = make()[1]
    assert "### CRITICAL: DNS cannot resolve vast.ai" in markdown
    assert "network.dns\\_vast\\_ai\\_resolves = False" in markdown
    assert "What to do:" in markdown and "Nothing was uploaded" in markdown
    assert "Diagnosis: INCOMPLETE" in markdown


def test_the_markdown_renderer_handles_an_empty_report() -> None:
    markdown = render_markdown(Report.model_validate_json(make(diagnosis())[0]))
    assert "No problems were found in the checks that ran." in markdown


def test_unicode_and_odd_whitespace_cannot_break_the_json() -> None:
    diag = diagnosis(F("docker.server_version", "27.3 .1\u0085 ퟿ café"))
    json_text, _ = make(diag)
    json.loads(json_text)
    assert json_text.isascii()


# -- writing files ---------------------------------------------------------------------------------------------


def test_both_files_are_created_with_the_same_timestamped_name(tmp_path: Path) -> None:
    json_path, md_path = write_report(tmp_path / "out", "{}\n", "# md\n", NOW)
    assert json_path.name == "report-20261123T120000Z.json"
    assert md_path.name == "report-20261123T120000Z.md"
    assert json_path.read_text() == "{}\n" and md_path.read_text() == "# md\n"
    assert json_path.is_absolute() and md_path.is_absolute()


def test_a_name_collision_gets_a_suffix_and_never_overwrites(tmp_path: Path) -> None:
    first = write_report(tmp_path, "one", "one", NOW)
    second = write_report(tmp_path, "two", "two", NOW)
    third = write_report(tmp_path, "three", "three", NOW)
    assert first[0].read_text() == "one"  # untouched
    assert second[0].name.endswith("-1.json") and third[0].name.endswith("-2.json")
    assert second[1].name.endswith("-1.md")


def test_a_half_collision_skips_to_the_next_name(tmp_path: Path) -> None:
    (tmp_path / "report-20261123T120000Z.md").write_text("someone else's")
    json_path, _ = write_report(tmp_path, "{}", "x", NOW)
    assert json_path.name.endswith("-1.json")
    assert (tmp_path / "report-20261123T120000Z.md").read_text() == "someone else's"


def test_too_many_collisions_is_an_error_not_an_overwrite(tmp_path: Path) -> None:
    for n in range(100):
        suffix = "" if n == 0 else f"-{n}"
        (tmp_path / f"report-20261123T120000Z{suffix}.json").write_text("x")
    with pytest.raises(ReportError, match="too many"):
        write_report(tmp_path, "{}", "x", NOW)


def test_no_half_report_is_left_if_the_second_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = report_module.create_private_file

    def fail_on_markdown(path: Path, data: bytes) -> None:
        if path.suffix == ".md":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(report_module, "create_private_file", fail_on_markdown)
    with pytest.raises(ReportError, match="disk full"):
        write_report(tmp_path, "{}", "x", NOW)
    assert list(tmp_path.iterdir()) == []


def test_an_output_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(ReportError, match="output directory"):
        write_report(target, "{}", "x", NOW)
    assert target.read_text() == "x"


@posix_only
def test_files_are_owner_only_and_the_new_directory_is_private(tmp_path: Path) -> None:
    json_path, md_path = write_report(tmp_path / "new" / "reports", "{}", "x", NOW)
    for path in (json_path, md_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(json_path.parent.stat().st_mode) == 0o700


@posix_only
def test_a_symlinked_output_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ReportError):
        write_report(link, "{}", "x", NOW)
    assert list(real.iterdir()) == []


@posix_only
def test_a_symlink_where_a_report_file_would_go_is_never_followed(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (tmp_path / "report-20261123T120000Z.json").symlink_to(victim)
    json_path, _ = write_report(tmp_path, "{}", "x", NOW)
    assert victim.read_text() == "keep me"
    assert json_path.name.endswith("-1.json")


@posix_only
def test_a_directory_other_users_can_write_to_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(OSError, match="writable by other users"):
        ensure_private_dir(shared)


def test_create_private_file_never_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "f"
    create_private_file(target, b"first")
    with pytest.raises(FileExistsError):
        create_private_file(target, b"second")
    assert target.read_bytes() == b"first"


def test_build_report_does_not_mutate_the_diagnosis() -> None:
    diag = broken_host()
    before = [f.model_dump_json() for f in diag.facts]
    build_report(diag, None, NOW)
    assert [f.model_dump_json() for f in diag.facts] == before


# -- found by independent testing (F-13, F-14) -----------------------------------------------------


def test_host_fields_and_evidence_are_capped_to_the_shareable_limit() -> None:
    """F-13: a 40,000-character OS name used to bypass the 200-character limit."""
    huge = "x" * 40_000
    diag = diagnosis(
        F("system.os_pretty_name", huge),
        F("system.kernel_release", huge),
        F("network.dns_vast_ai_resolves", False),
    )
    report = Report.model_validate_json(make(diag)[0])
    assert len(report.host.os or "") <= 200 and len(report.host.kernel or "") <= 200


def test_a_custom_docker_root_directory_is_not_exported() -> None:
    import json as _json

    from vast_troubleshooter.collectors.docker import DockerCollector
    from vast_troubleshooter.runner import CommandResult, CommandStatus
    from vast_troubleshooter.testing import fake_context

    info = _json.dumps(
        {
            "ServerVersion": "27.3.1",
            "DefaultRuntime": "runc",
            "Runtimes": {"runc": {}},
            "DockerRootDir": "/srv/acme-corp-customer-42/docker",
        }
    )
    ctx = fake_context(
        results={
            "docker.info": CommandResult(
                "docker.info", CommandStatus.OK, returncode=0, stdout=info
            )
        }
    )
    facts = DockerCollector().collect(ctx)
    diag = diagnosis(*facts)
    json_text, md_text = make(diag)
    assert "acme-corp-customer-42" not in json_text + md_text
    assert {f.key: f.value for f in facts}["docker.root_dir"] == "custom location"
