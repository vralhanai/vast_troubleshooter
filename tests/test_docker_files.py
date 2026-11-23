"""The Docker files are the security boundary for dev and test. These tests read
them as text and fail if anyone weakens the defaults."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if not (ROOT / "Dockerfile").exists() or not (ROOT / "docs" / "docker-dev.md").exists():
    # The test image contains only src/ and tests/. These checks run in the repository.
    pytest.skip("repository files are not present here", allow_module_level=True)
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()
GPU = (ROOT / "docker-compose.gpu.yml").read_text()
IGNORE = (ROOT / ".dockerignore").read_text()
DOC = (ROOT / "docs" / "docker-dev.md").read_text()


def code(text: str) -> str:
    """The file without comment lines, so a comment can mention a forbidden word."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def stage(name: str) -> str:
    match = re.search(
        rf"^FROM .* AS {name}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.DOTALL | re.MULTILINE
    )
    assert match, f"no stage {name}"
    return match.group(1)


# -- Dockerfile ------------------------------------------------------------------------------------


def test_base_images_are_pinned_by_digest() -> None:
    for arg in ("UBUNTU", "UV"):
        line = re.search(rf"^ARG {arg}=(\S+)", DOCKERFILE, re.MULTILINE)
        assert line and re.search(r"@sha256:[0-9a-f]{64}$", line.group(1)), arg


def test_the_runtime_image_runs_as_a_non_root_user() -> None:
    runtime = stage("runtime")
    assert re.search(r"^USER vast$", runtime, re.MULTILINE)
    assert runtime.index("USER vast") < runtime.index("ENTRYPOINT")
    assert "useradd --uid 10001" in runtime


def test_the_test_image_also_runs_as_a_non_root_user() -> None:
    test = stage("test")
    assert re.search(r"^USER vast$", test, re.MULTILINE)
    assert test.index("USER vast") < test.index("CMD")


def test_the_runtime_image_has_no_build_tools_no_tests_and_no_package_manager() -> None:
    runtime = stage("runtime")
    assert "COPY tests" not in runtime and "uv" not in code(runtime).replace(
        "--from=builder", ""
    )
    assert "build-essential" not in runtime and "gcc" not in runtime


def test_dependencies_are_installed_exactly_from_the_lock_file() -> None:
    assert code(DOCKERFILE).count("--frozen") >= 3
    assert "pip install" not in code(DOCKERFILE)


def test_nothing_is_piped_into_a_shell_and_nothing_uses_sudo() -> None:
    body = code(DOCKERFILE)
    assert not re.search(r"(curl|wget)[^\n]*\|\s*(ba)?sh", body)
    assert "sudo" not in body


def test_the_entrypoint_is_the_tool_and_the_default_is_harmless() -> None:
    assert 'ENTRYPOINT ["vast-troubleshooter"]' in DOCKERFILE
    assert 'CMD ["--help"]' in DOCKERFILE


# -- docker-compose.yml --------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [COMPOSE, GPU], ids=["compose", "gpu"])
@pytest.mark.parametrize(
    "forbidden",
    [
        "privileged",
        "docker.sock",
        "pid: host",
        'pid: "host"',
        "network_mode",
        "ipc: host",
        "userns_mode",
        "cap_add",
        "security_opt:\n      - seccomp:unconfined",
        "apparmor:unconfined",
        "/var/run",
        "/proc",
        "/sys",
        "/dev",
    ],
)
def test_nothing_weakens_the_sandbox(text: str, forbidden: str) -> None:
    assert forbidden not in code(text)


def test_the_main_service_is_hardened() -> None:
    body = code(COMPOSE)
    assert 'user: "10001:10001"' in body
    assert "read_only: true" in body
    assert re.search(r"cap_drop:\s*\n\s*- ALL", body)
    assert "no-new-privileges:true" in body
    assert "pids_limit" in body and "mem_limit" in body
    assert "noexec" in body  # the tmpfs mounts cannot run programs


def test_there_are_no_host_folder_mounts() -> None:
    for line in code(COMPOSE).splitlines():
        item = line.strip()
        if item.startswith("- ") and ":" in item:
            source = item[2:].split(":", 1)[0].strip('"')
            # named volumes and tmpfs targets only: never a path or a drive
            assert not source.startswith(("/", ".", "~")) or item.startswith(
                ("- /tmp", "- /home/vast")
            ), item
            assert not re.match(r"^[A-Za-z]$", source), item


def test_reports_go_to_a_named_volume_not_a_host_folder() -> None:
    assert "- reports:/reports" in code(COMPOSE)
    assert re.search(r"^volumes:\s*\n\s+reports: \{\}", code(COMPOSE), re.MULTILINE)


def test_the_tests_service_is_also_unprivileged() -> None:
    tests = COMPOSE.split("  tests:")[1]
    assert (
        'user: "10001:10001"' in tests
        and "cap_drop" in tests
        and "no-new-privileges" in tests
    )


# -- the GPU override ------------------------------------------------------------------------------------


def test_the_gpu_override_only_adds_the_nvidia_device_request() -> None:
    body = code(GPU)
    assert "driver: nvidia" in body and "capabilities: [gpu]" in body
    for key in (
        "read_only",
        "cap_drop",
        "user:",
        "volumes:",
        "environment:",
        "command:",
    ):
        assert key not in body  # it overrides nothing else
    assert "CONTAINER / WSL observation" in GPU  # the file says what its output means


# -- the build context and the docs --------------------------------------------------------------------------


@pytest.mark.parametrize("entry", [".git", ".venv", ".env", ".claude", "reports"])
def test_the_build_context_leaves_out_local_and_private_files(entry: str) -> None:
    assert entry in IGNORE.splitlines()


def test_the_docs_record_the_observed_limits() -> None:
    for needle in (
        "no adapters were found",  # the real GPU error seen on this machine
        "container on a WSL2 kernel",
        "--read-only",
        "docker compose run --rm tests",
        "digest",
    ):
        assert needle in DOC
