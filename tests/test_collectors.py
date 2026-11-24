from pathlib import Path

import pytest

from vast_troubleshooter.collectors import (
    COLLECTORS,
    UnknownCollectorError,
    get_collectors,
    run_collectors,
)
from vast_troubleshooter.collectors.base import Collector, fact_status
from vast_troubleshooter.collectors.system import (
    OS_RELEASE,
    PROC_UPTIME,
    SystemCollector,
    parse_os_release,
)
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.files import FileNotAllowedError, LocalFiles
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeFiles, FakeRunner, fake_context

FIXTURES = Path(__file__).parent / "fixtures" / "system"


def load(release: str) -> HostContext:
    folder = FIXTURES / release
    return fake_context(
        results={"system.uname_release": (folder / "uname_r").read_text()},
        files={
            OS_RELEASE: (folder / "os-release").read_text(),
            PROC_UPTIME: (folder / "proc_uptime").read_text(),
            "/proc/version": "Linux version 6.8.0-45-generic (buildd@host) #45-Ubuntu SMP\n",
        },
    )


def by_key(facts: list[Fact]) -> dict[str, Fact]:
    return {f.key: f for f in facts}


# -- system collector against both fixtures ------------------------------------


@pytest.mark.parametrize(
    ("release", "version_id", "pretty", "kernel", "uptime"),
    [
        (
            "ubuntu_22_04",
            "22.04",
            "Ubuntu 22.04.5 LTS",
            "5.15.0-125-generic",
            123456.78,
        ),
        ("ubuntu_24_04", "24.04", "Ubuntu 24.04.1 LTS", "6.8.0-45-generic", 3600.25),
    ],
)
def test_system_collector_reads_ubuntu_fixtures(
    release: str, version_id: str, pretty: str, kernel: str, uptime: float
) -> None:
    facts = by_key(SystemCollector().collect(load(release)))
    assert facts["system.os_id"].value == "ubuntu"
    assert facts["system.os_version_id"].value == version_id
    assert facts["system.os_pretty_name"].value == pretty
    assert facts["system.kernel_release"].value == kernel
    assert facts["system.uptime_seconds"].value == uptime
    assert facts["system.uptime_seconds"].unit == "s"
    assert all(f.status is FactStatus.OK for f in facts.values())
    assert all(f.collector == "system" for f in facts.values())


def test_system_facts_are_safe_to_share() -> None:
    for fact in SystemCollector().collect(load("ubuntu_24_04")):
        fact.to_shareable()  # raises if a value is unsafe


def test_missing_os_release_is_a_missing_fact() -> None:
    ctx = fake_context(
        results={"system.uname_release": "6.8.0-45-generic\n"},
        files={PROC_UPTIME: "10.0 20.0\n"},
    )
    facts = by_key(SystemCollector().collect(ctx))
    assert facts["system.os_release"].status is FactStatus.MISSING
    assert "system.os_id" not in facts
    assert facts["system.kernel_release"].status is FactStatus.OK


def test_os_release_without_version_id_marks_only_that_field_missing() -> None:
    ctx = fake_context(
        results={"system.uname_release": "6.8.0\n"},
        files={OS_RELEASE: 'ID=ubuntu\nPRETTY_NAME="Ubuntu"\n', PROC_UPTIME: "1 2\n"},
    )
    facts = by_key(SystemCollector().collect(ctx))
    assert facts["system.os_id"].status is FactStatus.OK
    assert facts["system.os_version_id"].status is FactStatus.MISSING


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            CommandResult("system.uname_release", CommandStatus.TIMEOUT),
            FactStatus.TIMEOUT,
        ),
        (
            CommandResult("system.uname_release", CommandStatus.MISSING),
            FactStatus.MISSING,
        ),
        (
            CommandResult(
                "system.uname_release", CommandStatus.ERROR, stderr="bad", returncode=1
            ),
            FactStatus.ERROR,
        ),
        (
            CommandResult("system.uname_release", CommandStatus.REFUSED),
            FactStatus.ERROR,
        ),
    ],
)
def test_failed_uname_becomes_a_non_ok_fact(
    result: CommandResult, expected: FactStatus
) -> None:
    ctx = fake_context(
        results={"system.uname_release": result},
        files={OS_RELEASE: "ID=ubuntu\n", PROC_UPTIME: "1 2\n"},
    )
    fact = by_key(SystemCollector().collect(ctx))["system.kernel_release"]
    assert fact.status is expected
    assert fact.value is None


def test_empty_uname_output_is_an_error() -> None:
    ctx = fake_context(results={"system.uname_release": "\n"})
    fact = by_key(SystemCollector().collect(ctx))["system.kernel_release"]
    assert fact.status is FactStatus.ERROR


@pytest.mark.parametrize("text", ["", "abc def\n", "\n"])
def test_unparseable_uptime_is_an_error_fact(text: str) -> None:
    ctx = fake_context(files={PROC_UPTIME: text})
    fact = by_key(SystemCollector().collect(ctx))["system.uptime_seconds"]
    assert fact.status is FactStatus.ERROR


def test_unreadable_file_is_an_error_fact_not_a_crash() -> None:
    ctx = fake_context(files={OS_RELEASE: PermissionError("denied")})
    fact = by_key(SystemCollector().collect(ctx))["system.os_release"]
    assert fact.status is FactStatus.ERROR
    assert "denied" in (fact.raw_excerpt or "")


def test_parse_os_release_handles_quotes_comments_and_blank_lines() -> None:
    values = parse_os_release("# note\n\nID=ubuntu\nNAME=\"Ubuntu\"\nX='y'\nbroken\n")
    assert values == {"ID": "ubuntu", "NAME": "Ubuntu", "X": "y"}


# -- runner loop ----------------------------------------------------------------


class Exploding(Collector):
    name = "exploding"

    def collect(self, ctx: HostContext) -> list[Fact]:
        raise RuntimeError("boom")


class Fine(Collector):
    name = "fine"

    def collect(self, ctx: HostContext) -> list[Fact]:
        return [self.fact("fine.value", 1)]


def test_one_raising_collector_does_not_stop_the_others() -> None:
    facts = run_collectors([Fine(), Exploding(), Fine()], fake_context())
    keys = [f.key for f in facts]
    assert keys == ["fine.value", "exploding.collector", "fine.value"]
    failed = facts[1]
    assert failed.status is FactStatus.ERROR
    assert "RuntimeError: boom" in (failed.raw_excerpt or "")


def test_fact_status_covers_every_command_status() -> None:
    for status in CommandStatus:
        assert fact_status(CommandResult("x", status)) in FactStatus


def test_registry_contains_system_and_rejects_unknown_names() -> None:
    assert "system" in COLLECTORS
    assert [c.name for c in get_collectors(["system"])] == ["system"]
    assert [c.name for c in get_collectors()] == list(COLLECTORS)
    with pytest.raises(UnknownCollectorError) as info:
        get_collectors(["nope"])
    assert "system" in str(info.value)


# -- allowlisted file access ----------------------------------------------------


def test_local_files_refuse_paths_outside_the_allowlist(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    with pytest.raises(FileNotAllowedError):
        LocalFiles(allowed={"/etc/os-release"}).read_text(str(secret))


def test_local_files_read_allowed_paths_with_a_size_limit(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("a" * 1000)
    files = LocalFiles(allowed={str(target)}, max_bytes=100)
    assert files.read_text(str(target)) == "a" * 100


def test_local_files_missing_allowed_path_raises_file_not_found(tmp_path: Path) -> None:
    path = str(tmp_path / "absent")
    with pytest.raises(FileNotFoundError):
        LocalFiles(allowed={path}).read_text(path)


def test_fakes_behave_like_the_real_thing() -> None:
    assert FakeFiles({"/a": "x"}).read_text("/a") == "x"
    with pytest.raises(FileNotFoundError):
        FakeFiles({}).read_text("/a")
    runner = FakeRunner({})
    assert runner.run("anything").status is CommandStatus.REFUSED
