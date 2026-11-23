"""Detecting that the tool runs in a container or on WSL, and saying so."""

import json

import pytest
from typer.testing import CliRunner

from vast_troubleshooter import cli
from vast_troubleshooter.collectors.system import SystemCollector
from vast_troubleshooter.diagnose import Diagnosis
from vast_troubleshooter.files import DEFAULT_READABLE_FILES
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.redact import Redactor
from vast_troubleshooter.report import Report, make_report_files
from vast_troubleshooter.rule_engine import evaluate_rules, load_default_rules
from vast_troubleshooter.testing import fake_context

PLAIN = "Linux version 6.8.0-45-generic (buildd@lcy02) #45-Ubuntu SMP\n"
WSL = "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@build) #1 SMP\n"
runner = CliRunner()


def env_facts(files: dict[str, str | Exception]) -> dict[str, Fact]:
    facts = SystemCollector().collect(fake_context(files=files))
    return {
        f.key: f
        for f in facts
        if f.key.startswith(
            ("system.in_container", "system.on_wsl", "system.environment")
        )
    }


@pytest.mark.parametrize(
    ("files", "in_container", "wsl", "label"),
    [
        ({"/proc/version": PLAIN}, False, False, "physical or virtual machine"),
        ({"/proc/version": PLAIN, "/.dockerenv": ""}, True, False, "container"),
        ({"/proc/version": PLAIN, "/run/.containerenv": ""}, True, False, "container"),
        ({"/proc/version": WSL}, False, True, "WSL2"),
        (
            {"/proc/version": WSL, "/.dockerenv": ""},
            True,
            True,
            "container on a WSL2 kernel",
        ),
    ],
)
def test_the_environment_is_told_from_the_markers(
    files, in_container, wsl, label
) -> None:
    facts = env_facts(files)
    assert facts["system.in_container"].value is in_container
    assert facts["system.on_wsl_kernel"].value is wsl
    assert facts["system.environment"].value == label
    assert all(f.status is FactStatus.OK for f in facts.values())


def test_without_proc_version_nothing_is_claimed() -> None:
    facts = env_facts({})  # for example, a Windows machine
    assert all(
        f.status is FactStatus.MISSING and f.value is None for f in facts.values()
    )


def test_an_unreadable_proc_version_is_an_error_not_a_claim() -> None:
    facts = env_facts({"/proc/version": PermissionError("denied")})
    assert all(f.status is FactStatus.ERROR and f.value is None for f in facts.values())


def test_an_unreadable_marker_is_simply_not_seen() -> None:
    facts = env_facts(
        {"/proc/version": PLAIN, "/.dockerenv": PermissionError("denied")}
    )
    assert facts["system.in_container"].value is False


def test_the_marker_files_are_on_the_read_allowlist() -> None:
    assert {"/proc/version", "/.dockerenv", "/run/.containerenv"} <= set(
        DEFAULT_READABLE_FILES
    )


def test_the_environment_facts_are_safe_to_share() -> None:
    for fact in env_facts({"/proc/version": WSL, "/.dockerenv": ""}).values():
        fact.to_shareable()


# -- saying so in the output -----------------------------------------------------------------------


def diagnosis(environment: str) -> Diagnosis:
    facts = (Fact(collector="system", key="system.environment", value=environment),)
    return Diagnosis(facts, tuple(evaluate_rules(load_default_rules(), facts)))


def test_a_container_is_named_by_the_diagnosis() -> None:
    assert diagnosis("container").environment == "container"
    assert (
        diagnosis("container on a WSL2 kernel").environment
        == "container on a WSL2 kernel"
    )
    assert diagnosis("WSL2").environment == "WSL2"


def test_a_plain_host_and_an_unknown_environment_say_nothing() -> None:
    assert diagnosis("physical or virtual machine").environment is None
    unknown = Diagnosis(
        (Fact(collector="s", key="system.environment", status=FactStatus.MISSING),), ()
    )
    assert unknown.environment is None
    assert Diagnosis((), ()).environment is None


def run_diagnose(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, str | Exception], *args: str
):
    monkeypatch.setattr(cli, "default_context", lambda **kw: fake_context(files=files))
    return runner.invoke(cli.app, ["diagnose", *args])


def test_diagnose_warns_that_a_container_is_not_a_vast_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_diagnose(monkeypatch, {"/proc/version": WSL, "/.dockerenv": ""})
    assert "Environment: container on a WSL2 kernel." in result.stdout
    assert "not a Vast host" in result.stdout
    assert result.stdout.index("Environment:") < result.stdout.index("checks:")


def test_diagnose_says_nothing_extra_on_a_plain_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_diagnose(monkeypatch, {"/proc/version": PLAIN})
    assert "Environment:" not in result.stdout


def test_the_json_output_carries_the_environment_as_a_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_diagnose(
        monkeypatch, {"/proc/version": WSL, "/.dockerenv": ""}, "--json"
    )
    facts = {f["key"]: f for f in json.loads(result.stdout)["facts"]}
    assert facts["system.environment"]["value"] == "container on a WSL2 kernel"


def test_the_report_names_the_environment() -> None:
    redactor = Redactor()
    json_text, markdown = make_report_files(
        diagnosis("container on a WSL2 kernel"), redactor
    )
    assert (
        Report.model_validate_json(json_text).host.environment
        == "container on a WSL2 kernel"
    )
    assert "- Environment: container on a WSL2 kernel" in markdown
