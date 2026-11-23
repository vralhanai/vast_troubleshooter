from pathlib import Path

import pytest

from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.docker import KEYS, DockerCollector
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import (
    TRUSTED_DOCKER_ENDPOINT,
    CommandResult,
    CommandStatus,
    SubprocessRunner,
)
from vast_troubleshooter.testing import fake_context

FIXTURES = Path(__file__).parent / "fixtures" / "docker"
OP = "docker.info"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def run(result: CommandResult) -> dict[str, Fact]:
    facts = DockerCollector().collect(fake_context(results={OP: result}))
    assert [f.key for f in facts][: len(KEYS)] == list(KEYS)
    return {f.key: f for f in facts}


def ok(text: str) -> CommandResult:
    return CommandResult(OP, CommandStatus.OK, returncode=0, stdout=text)


def failed(
    text: str = "",
    status: CommandStatus = CommandStatus.ERROR,
    stderr: str = "",
    detail: str = "",
) -> CommandResult:
    return CommandResult(
        OP, status, returncode=1, stdout=text, stderr=stderr, detail=detail
    )


def test_running_with_nvidia_runtime() -> None:
    facts = run(ok(fixture("running_nvidia.json")))
    assert facts["docker.daemon_running"].value is True
    assert facts["docker.server_version"].value == "27.3.1"
    assert facts["docker.nvidia_runtime_present"].value is True
    assert facts["docker.runtimes"].value == "io.containerd.runc.v2,nvidia,runc"
    assert facts["docker.default_runtime"].value == "runc"
    assert facts["docker.root_dir"].value == "/var/lib/docker"
    assert all(f.status is FactStatus.OK for f in facts.values())


def test_running_without_nvidia_runtime() -> None:
    facts = run(ok(fixture("running_no_nvidia.json")))
    assert facts["docker.daemon_running"].value is True
    assert facts["docker.nvidia_runtime_present"].value is False
    assert facts["docker.nvidia_runtime_present"].status is FactStatus.OK


def test_daemon_not_running_is_an_observation_not_an_unknown() -> None:
    facts = run(failed(fixture("daemon_down.json")))
    running = facts["docker.daemon_running"]
    assert running.value is False
    assert running.status is FactStatus.OK
    # Everything else is unknown while the daemon is down.
    for key in KEYS[1:]:
        assert facts[key].status is FactStatus.ERROR
        assert facts[key].value is None


def test_daemon_down_reported_only_on_stderr() -> None:
    result = failed(
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
    )
    assert run(result)["docker.daemon_running"].value is False


def test_permission_denied_is_unknown_not_stopped() -> None:
    facts = run(failed(fixture("permission_denied.json")))
    running = facts["docker.daemon_running"]
    assert running.value is None
    assert running.status is FactStatus.ERROR
    assert "permission denied" in (running.raw_excerpt or "")


def test_docker_not_installed() -> None:
    facts = run(CommandResult(OP, CommandStatus.MISSING, detail="docker not found"))
    assert all(f.status is FactStatus.MISSING for f in facts.values())


def test_hang_is_a_timeout_for_every_fact() -> None:
    facts = run(failed(status=CommandStatus.TIMEOUT, detail="timed out after 20.0s"))
    assert all(f.status is FactStatus.TIMEOUT for f in facts.values())


def test_refused_endpoint_override_is_an_error_not_a_stopped_daemon() -> None:
    facts = run(
        CommandResult(
            OP,
            CommandStatus.REFUSED,
            detail="DOCKER_HOST points away from the trusted local endpoint",
        )
    )
    assert all(f.status is FactStatus.ERROR for f in facts.values())
    assert facts["docker.daemon_running"].value is None
    assert "DOCKER_HOST" in (facts["docker.daemon_running"].raw_excerpt or "")


def test_unparseable_output_is_an_error() -> None:
    facts = run(ok("this is not json"))
    assert facts["docker.daemon_running"].status is FactStatus.ERROR
    assert facts["docker.daemon_running"].value is None


def test_unexpected_failure_is_an_error() -> None:
    facts = run(failed(stderr="something odd happened"))
    assert facts["docker.daemon_running"].status is FactStatus.ERROR


def test_json_without_runtimes_marks_those_facts_missing() -> None:
    facts = run(ok('{"ServerVersion": "27.3.1", "ServerErrors": null}'))
    assert facts["docker.daemon_running"].value is True
    assert facts["docker.runtimes"].status is FactStatus.MISSING
    assert facts["docker.nvidia_runtime_present"].status is FactStatus.MISSING


def test_facts_are_safe_to_share() -> None:
    for name in ("running_nvidia.json", "running_no_nvidia.json"):
        for fact in run(ok(fixture(name))).values():
            fact.to_shareable()


# -- trusted endpoint (runner policy applied to the real operation) -------------


def fake_which(name: str, path: str | None) -> str:
    return f"/usr/bin/{name}"


def test_docker_info_is_pinned_to_the_local_socket() -> None:
    runner = SubprocessRunner(READ_OPERATIONS, environ={}, which=fake_which)
    argv, early = runner.plan(OP)
    assert early is None
    assert argv == [
        "/usr/bin/docker",
        "--host",
        TRUSTED_DOCKER_ENDPOINT,
        "info",
        "--format",
        "{{json .}}",
    ]


@pytest.mark.parametrize(
    "environ",
    [{"DOCKER_HOST": "tcp://evil.example:2375"}, {"DOCKER_CONTEXT": "remote"}],
)
def test_docker_info_refused_when_environment_points_elsewhere(
    environ: dict[str, str],
) -> None:
    runner = SubprocessRunner(READ_OPERATIONS, environ=environ, which=fake_which)
    argv, early = runner.plan(OP)
    assert argv == []
    assert early is not None
    assert early.status is CommandStatus.REFUSED


def test_registry_includes_docker() -> None:
    assert "docker" in COLLECTORS


def test_daemon_down_in_the_shape_seen_on_a_real_docker_client() -> None:
    # Observed with Docker 29.9.3: exit 1, empty JSON fields, ServerErrors null,
    # and the message on stderr only.
    result = failed(
        fixture("daemon_down_observed.json"),
        stderr=(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?"
        ),
    )
    facts = run(result)
    assert facts["docker.daemon_running"].value is False
    assert facts["docker.daemon_running"].status is FactStatus.OK
    assert facts["docker.nvidia_runtime_present"].status is FactStatus.ERROR
