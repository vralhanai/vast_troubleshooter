import re
import subprocess
import sys
import time

import pytest

from vast_troubleshooter import runner as runner_module
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import (
    TRUSTED_DOCKER_ENDPOINT,
    CommandStatus,
    Operation,
    SubprocessRunner,
)
from vast_troubleshooter.testing import FakeRunner


def py_op(op_id: str, code: str, **kwargs: object) -> Operation:
    return Operation(
        id=op_id,
        executable=sys.executable,
        args=("-I", "-c", code),
        **kwargs,  # type: ignore[arg-type]
    )


def make_runner(*ops: Operation, **kwargs: object) -> SubprocessRunner:
    return SubprocessRunner({op.id: op for op in ops}, **kwargs)  # type: ignore[arg-type]


# -- real process behavior -----------------------------------------------------


def test_allowlisted_command_runs() -> None:
    r = make_runner(py_op("t.ok", "print('hello')"))
    result = r.run("t.ok")
    assert result.status is CommandStatus.OK
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.returncode == 0
    assert result.duration >= 0


def test_unknown_operation_is_refused() -> None:
    result = make_runner(py_op("t.ok", "pass")).run("t.other")
    assert result.status is CommandStatus.REFUSED
    assert "allowlisted" in result.detail


def test_timeout_kills_the_command_and_returns_promptly() -> None:
    r = make_runner(py_op("t.slow", "import time; time.sleep(30)", timeout=0.5))
    start = time.monotonic()
    result = r.run("t.slow")
    assert result.status is CommandStatus.TIMEOUT
    assert time.monotonic() - start < 10


def test_missing_binary() -> None:
    op = Operation(id="t.missing", executable="definitely-not-a-real-binary-xyz")
    result = make_runner(op).run("t.missing")
    assert result.status is CommandStatus.MISSING


def test_missing_absolute_binary() -> None:
    op = Operation(id="t.missing", executable=str(sys.executable) + ".nope")
    result = make_runner(op).run("t.missing")
    assert result.status in {CommandStatus.MISSING, CommandStatus.ERROR}


def test_non_zero_exit_is_an_error_with_stderr() -> None:
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    result = make_runner(py_op("t.fail", code)).run("t.fail")
    assert result.status is CommandStatus.ERROR
    assert result.returncode == 3
    assert "boom" in result.stderr


def test_output_is_bounded() -> None:
    r = make_runner(py_op("t.big", "print('x' * 100000)", max_output=100))
    result = r.run("t.big")
    assert result.status is CommandStatus.OK
    assert result.truncated
    assert len(result.stdout) <= 100


def test_child_gets_a_minimal_environment() -> None:
    code = "import os; print(sorted(os.environ)); print(os.environ.get('LC_ALL'))"
    r = make_runner(
        py_op("t.env", code),
        environ={"SECRET_TOKEN": "hunter2", "DOCKER_HOST": ""},
    )
    result = r.run("t.env")
    assert result.status is CommandStatus.OK
    assert "SECRET_TOKEN" not in result.stdout
    assert "hunter2" not in result.stdout
    assert result.stdout.strip().endswith("C")


# -- no shell, operands ---------------------------------------------------------


def test_popen_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    real_popen = subprocess.Popen

    def recording_popen(argv: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((argv, kwargs))
        return real_popen(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(runner_module.subprocess, "Popen", recording_popen)
    make_runner(py_op("t.ok", "print(1)")).run("t.ok")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert kwargs["shell"] is False


@pytest.mark.parametrize(
    "value", ["x; rm -rf /", "a b", "$(id)", "`id`", "../etc", "", "x\x00y"]
)
def test_operand_with_shell_metacharacters_is_refused(value: str) -> None:
    op = Operation(
        id="t.dev",
        executable=sys.executable,
        args=("-I", "-c", "print(1)", "{device}"),
        operands={"device": re.compile(r"[a-z0-9]+")},
    )
    result = make_runner(op).run("t.dev", device=value)
    assert result.status is CommandStatus.REFUSED


def test_operand_that_looks_like_an_option_is_refused() -> None:
    op = Operation(
        id="t.dev",
        executable=sys.executable,
        args=("-I", "-c", "print(1)", "{device}"),
        operands={"device": re.compile(r"[a-z0-9-]+")},
    )
    result = make_runner(op).run("t.dev", device="--help")
    assert result.status is CommandStatus.REFUSED
    assert "option" in result.detail


def test_valid_operand_is_substituted_as_one_argument() -> None:
    op = Operation(
        id="t.dev",
        executable=sys.executable,
        args=("-I", "-c", "import sys; print(sys.argv[1:])", "{device}"),
        operands={"device": re.compile(r"[a-z0-9]+")},
    )
    result = make_runner(op).run("t.dev", device="nvme0")
    assert result.status is CommandStatus.OK
    assert "['nvme0']" in result.stdout


def test_missing_or_extra_operands_are_refused() -> None:
    op = Operation(
        id="t.dev",
        executable=sys.executable,
        args=("-I", "-c", "print(1)", "{device}"),
        operands={"device": re.compile(r"[a-z0-9]+")},
    )
    r = make_runner(op, py_op("t.none", "print(1)"))
    assert r.run("t.dev").status is CommandStatus.REFUSED
    assert r.run("t.dev", device="a", extra="b").status is CommandStatus.REFUSED
    assert r.run("t.none", extra="b").status is CommandStatus.REFUSED


# -- Docker endpoint policy -----------------------------------------------------

DOCKER_OP = Operation(id="t.docker", executable="docker", args=("info",), docker=True)
FAKE_WHICH = lambda name, path: f"/usr/bin/{name}"


def docker_runner(environ: dict[str, str]) -> SubprocessRunner:
    return make_runner(DOCKER_OP, environ=environ, which=FAKE_WHICH)


def test_docker_always_gets_the_trusted_local_endpoint() -> None:
    argv, early = docker_runner({}).plan("t.docker")
    assert early is None
    assert argv == ["/usr/bin/docker", "--host", TRUSTED_DOCKER_ENDPOINT, "info"]


@pytest.mark.parametrize(
    "environ",
    [
        {"DOCKER_HOST": "tcp://evil.example:2375"},
        {"DOCKER_HOST": "ssh://user@remote"},
        {"DOCKER_CONTEXT": "remote-prod"},
    ],
)
def test_docker_refused_when_environment_points_elsewhere(
    environ: dict[str, str],
) -> None:
    argv, early = docker_runner(environ).plan("t.docker")
    assert argv == []
    assert early is not None
    assert early.status is CommandStatus.REFUSED


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"DOCKER_HOST": TRUSTED_DOCKER_ENDPOINT},
        {"DOCKER_CONTEXT": "default"},
    ],
)
def test_docker_allowed_for_local_or_default_settings(environ: dict[str, str]) -> None:
    _, early = docker_runner(environ).plan("t.docker")
    assert early is None


# -- root handling --------------------------------------------------------------

ROOT_OP = Operation(
    id="t.root", executable="smartctl", args=("-H", "/dev/nvme0"), needs_root=True
)


def test_needs_root_is_reported_and_nothing_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(runner_module.subprocess, "Popen", boom)
    r = make_runner(ROOT_OP, is_root=lambda: False, which=FAKE_WHICH)
    result = r.run("t.root")
    assert result.status is CommandStatus.NEEDS_ROOT


def test_elevated_reads_use_fixed_sudo_prefix_when_allowed() -> None:
    r = make_runner(
        ROOT_OP, allow_elevated=True, is_root=lambda: False, which=FAKE_WHICH
    )
    argv, early = r.plan("t.root")
    assert early is None
    assert argv == [
        "/usr/bin/sudo",
        "-n",
        "--",
        "/usr/bin/smartctl",
        "-H",
        "/dev/nvme0",
    ]


def test_no_sudo_when_already_root() -> None:
    r = make_runner(ROOT_OP, is_root=lambda: True, which=FAKE_WHICH)
    argv, early = r.plan("t.root")
    assert early is None
    assert argv[0] == "/usr/bin/smartctl"


def test_needs_root_with_elevation_but_no_sudo() -> None:
    def which(name: str, path: str | None) -> str | None:
        return None if name == "sudo" else f"/usr/bin/{name}"

    r = make_runner(ROOT_OP, allow_elevated=True, is_root=lambda: False, which=which)
    _, early = r.plan("t.root")
    assert early is not None
    assert early.status is CommandStatus.NEEDS_ROOT


# -- operation definitions and registry -----------------------------------------


def test_operation_rejects_placeholder_without_operand() -> None:
    with pytest.raises(ValueError):
        Operation(id="x", executable="uname", args=("{device}",))


def test_operation_rejects_relative_path_executable() -> None:
    with pytest.raises(ValueError):
        Operation(id="x", executable="bin/uname")


def test_registry_has_only_valid_read_operations() -> None:
    assert "system.uname_release" in READ_OPERATIONS
    for op_id, op in READ_OPERATIONS.items():
        assert op.id == op_id
        assert not op.needs_root or op.executable  # constructed and validated


# -- fake runner ----------------------------------------------------------------


def test_fake_runner_returns_fixture_and_records_calls() -> None:
    fake = FakeRunner({"system.uname_release": "6.8.0-45-generic\n"})
    result = fake.run("system.uname_release")
    assert result.ok
    assert result.stdout == "6.8.0-45-generic\n"
    assert fake.calls == [("system.uname_release", {})]


def test_fake_runner_refuses_unknown_operation() -> None:
    result = FakeRunner({}).run("gpu.query")
    assert result.status is CommandStatus.REFUSED


def test_denied_permission_on_start_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(runner_module.subprocess, "Popen", denied)
    result = make_runner(py_op("t.ok", "print(1)")).run("t.ok")
    assert result.status is CommandStatus.ERROR
    assert "denied" in result.detail


def test_a_command_that_cannot_be_killed_does_not_hang_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-07: a refused kill (root process under sudo) used to block `proc.wait()` forever."""
    monkeypatch.setattr(runner_module, "_kill_tree", lambda proc: None)
    monkeypatch.setattr(runner_module, "_KILL_GRACE", 0.5)
    r = make_runner(py_op("t.stuck", "import time; time.sleep(8)", timeout=0.3))
    start = time.monotonic()
    result = r.run("t.stuck")
    assert time.monotonic() - start < 4  # returned; did not wait the full 8 seconds
    assert result.status is CommandStatus.TIMEOUT
    assert "could not be stopped" in result.detail
