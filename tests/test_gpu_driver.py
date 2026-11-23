from pathlib import Path

import pytest

from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.driver import DriverCollector, cuda_code
from vast_troubleshooter.collectors.gpu import (
    GpuCollector,
    decode_throttle,
    parse_field,
)
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeRunner, fake_context

FIXTURES = Path(__file__).parent / "fixtures" / "gpu"

MESSAGE = (
    "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."
)


def ok(op_id: str, text: str) -> CommandResult:
    return CommandResult(op_id, CommandStatus.OK, returncode=0, stdout=text)


def failed(
    op_id: str,
    status: CommandStatus = CommandStatus.ERROR,
    stdout: str = "",
    detail: str = "",
) -> CommandResult:
    return CommandResult(
        op_id,
        status,
        returncode=None if status is CommandStatus.TIMEOUT else 9,
        stdout=stdout,
        detail=detail,
    )


def fixture(case: str, name: str) -> str:
    return (FIXTURES / case / name).read_text()


def healthy(case: str) -> HostContext:
    results = {
        "gpu.query": ok("gpu.query", fixture(case, "query.csv")),
        "gpu.throttle_event": ok("gpu.throttle_event", fixture(case, "throttle.csv")),
        "gpu.lspci_nvidia": ok("gpu.lspci_nvidia", fixture(case, "lspci.txt")),
    }
    if (FIXTURES / case / "smi.txt").exists():  # not every case has a header
        results["driver.smi_summary"] = ok(
            "driver.smi_summary", fixture(case, "smi.txt")
        )
    return fake_context(results=results)


def by_key(facts: list[Fact]) -> dict[str, Fact]:
    return {f.key: f for f in facts}


def gpu_facts(ctx: HostContext) -> dict[str, Fact]:
    return by_key(GpuCollector().collect(ctx))


def driver_facts(ctx: HostContext) -> dict[str, Fact]:
    return by_key(DriverCollector().collect(ctx))


# -- healthy fixtures -----------------------------------------------------------


def test_healthy_single_gpu() -> None:
    facts = gpu_facts(healthy("healthy_1gpu"))
    assert facts["gpu.count"].value == 1
    assert facts["gpu.pci_count"].value == 1  # the audio function is not counted
    assert facts["gpu.0.name"].value == "NVIDIA GeForce RTX 4090"
    assert facts["gpu.0.memory_total"].value == 24564
    assert facts["gpu.0.memory_total"].unit == "MiB"
    assert facts["gpu.0.temperature"].value == 34
    assert facts["gpu.0.power_draw"].value == 54.2
    assert facts["gpu.0.clock_sm"].value == 210
    assert facts["gpu.0.clock_mem"].value == 10501
    assert facts["gpu.0.pcie_gen"].value == 4
    assert facts["gpu.0.pcie_width"].value == 16
    assert facts["gpu.0.throttle_reasons"].value == "gpu_idle"
    assert facts["gpu.models_identical"].value is True
    assert facts["gpu.vram_min_mib"].value == 24564


def test_consumer_gpu_ecc_is_unsupported_not_empty() -> None:
    fact = gpu_facts(healthy("healthy_1gpu"))["gpu.0.ecc_mode"]
    assert fact.status is FactStatus.UNSUPPORTED
    assert fact.value is None


def test_healthy_eight_gpus() -> None:
    facts = gpu_facts(healthy("healthy_8gpu"))
    assert facts["gpu.count"].value == 8
    assert facts["gpu.pci_count"].value == 8
    assert facts["gpu.models_identical"].value is True
    assert facts["gpu.vram_min_mib"].value == 81559
    for i in range(8):
        assert facts[f"gpu.{i}.ecc_mode"].value == "Enabled"
        assert facts[f"gpu.{i}.throttle_reasons"].value == "none"
        assert facts[f"gpu.{i}.pcie_gen"].value == 5


@pytest.mark.parametrize(
    ("case", "driver", "cuda", "code"),
    [
        ("healthy_1gpu", "595.91.07", "13.1", 13010),
        ("healthy_8gpu", "580.178.04", "13.0", 13000),
    ],
)
def test_driver_versions_from_header(
    case: str, driver: str, cuda: str, code: int
) -> None:
    facts = driver_facts(healthy(case))
    assert facts["driver.nvidia_smi_responding"].value is True
    assert facts["driver.version"].value == driver
    assert facts["driver.cuda_version"].value == cuda
    assert facts["driver.cuda_version_code"].value == code


def test_healthy_facts_are_safe_to_share() -> None:
    for case in ("healthy_1gpu", "healthy_8gpu"):
        ctx = healthy(case)
        for fact in [*GpuCollector().collect(ctx), *DriverCollector().collect(ctx)]:
            fact.to_shareable()


# -- failure fixtures -----------------------------------------------------------


def broken_driver_ctx() -> HostContext:
    bad = {
        op: failed(op, stdout=MESSAGE)
        for op in (
            "gpu.query",
            "gpu.throttle_event",
            "gpu.throttle_legacy",
            "driver.smi_summary",
        )
    }
    return fake_context(
        results={
            **bad,
            "gpu.lspci_nvidia": ok(
                "gpu.lspci_nvidia", fixture("failed_driver", "lspci.txt")
            ),
        }
    )


def test_failed_driver_gpu_facts() -> None:
    facts = gpu_facts(broken_driver_ctx())
    assert facts["gpu.count"].status is FactStatus.ERROR
    assert facts["gpu.count"].value is None
    assert "couldn't communicate" in (facts["gpu.count"].raw_excerpt or "")
    assert facts["gpu.pci_count"].value == 1  # hardware is still on the bus
    assert facts["gpu.throttle_reasons"].status is FactStatus.ERROR


def test_failed_driver_reports_not_responding_as_an_observation() -> None:
    facts = driver_facts(broken_driver_ctx())
    responding = facts["driver.nvidia_smi_responding"]
    assert responding.value is False
    assert responding.status is FactStatus.OK
    assert "cannot communicate" in (responding.raw_excerpt or "")
    assert facts["driver.version"].status is FactStatus.ERROR


def test_hang_is_a_timeout_and_not_responding() -> None:
    hung = {
        op: failed(op, CommandStatus.TIMEOUT, detail="timed out after 20.0s")
        for op in (
            "gpu.query",
            "gpu.throttle_event",
            "gpu.throttle_legacy",
            "driver.smi_summary",
        )
    }
    ctx = fake_context(results=hung)
    gpu = gpu_facts(ctx)
    assert gpu["gpu.count"].status is FactStatus.TIMEOUT
    driver = driver_facts(ctx)
    assert driver["driver.nvidia_smi_responding"].value is False
    assert "timed out" in (driver["driver.nvidia_smi_responding"].raw_excerpt or "")
    assert driver["driver.version"].status is FactStatus.TIMEOUT


def test_nvidia_smi_not_installed() -> None:
    missing = {
        op: CommandResult(op, CommandStatus.MISSING, detail="nvidia-smi not found")
        for op in (
            "gpu.query",
            "gpu.throttle_event",
            "gpu.throttle_legacy",
            "driver.smi_summary",
            "gpu.lspci_nvidia",
        )
    }
    ctx = fake_context(results=missing)
    gpu = gpu_facts(ctx)
    assert gpu["gpu.count"].status is FactStatus.MISSING
    assert gpu["gpu.pci_count"].status is FactStatus.MISSING
    driver = driver_facts(ctx)
    assert driver["driver.nvidia_smi_responding"].status is FactStatus.MISSING
    assert driver["driver.nvidia_smi_responding"].value is None


def test_err_and_unsupported_columns_are_marked_per_field() -> None:
    facts = gpu_facts(healthy("err_columns"))
    assert facts["gpu.count"].value == 2
    assert facts["gpu.0.temperature"].status is FactStatus.ERROR  # ERR!
    assert facts["gpu.0.power_draw"].status is FactStatus.UNSUPPORTED  # [N/A]
    assert facts["gpu.1.clock_sm"].status is FactStatus.ERROR  # [Unknown Error]
    assert facts["gpu.1.temperature"].value == 40  # other fields still read
    assert facts["gpu.0.name"].value == "NVIDIA A100-SXM4-80GB"
    assert facts["gpu.0.throttle_reasons"].status is FactStatus.UNSUPPORTED
    assert facts["gpu.1.throttle_reasons"].value == "sw_thermal_slowdown"


def test_fewer_gpus_than_on_the_pci_bus_shows_both_counts() -> None:
    facts = gpu_facts(healthy("fewer_gpus"))
    assert facts["gpu.count"].value == 7
    assert facts["gpu.pci_count"].value == 8


def test_no_devices_found_is_a_count_of_zero() -> None:
    ctx = fake_context(
        results={
            "gpu.query": failed("gpu.query", stdout="No devices were found"),
            "gpu.throttle_event": failed("gpu.throttle_event"),
            "gpu.throttle_legacy": failed("gpu.throttle_legacy"),
            "gpu.lspci_nvidia": ok("gpu.lspci_nvidia", ""),
        }
    )
    facts = gpu_facts(ctx)
    assert facts["gpu.count"].value == 0
    assert facts["gpu.count"].status is FactStatus.OK
    assert facts["gpu.pci_count"].value == 0


def test_row_with_wrong_field_count_is_an_error_fact() -> None:
    ctx = fake_context(
        results={
            "gpu.query": ok("gpu.query", "0, NVIDIA Thing, 1000\n"),
            "gpu.throttle_event": failed("gpu.throttle_event"),
            "gpu.throttle_legacy": failed("gpu.throttle_legacy"),
            "gpu.lspci_nvidia": ok("gpu.lspci_nvidia", ""),
        }
    )
    facts = gpu_facts(ctx)
    assert facts["gpu.0.row"].status is FactStatus.ERROR
    assert facts["gpu.count"].value == 0


def test_throttle_falls_back_to_the_legacy_field_name() -> None:
    runner = FakeRunner(
        {
            "gpu.query": ok("gpu.query", fixture("healthy_1gpu", "query.csv")),
            "gpu.throttle_event": failed("gpu.throttle_event", stdout="invalid field"),
            "gpu.throttle_legacy": ok("gpu.throttle_legacy", "0, 0x0000000000000040\n"),
            "gpu.lspci_nvidia": ok("gpu.lspci_nvidia", ""),
        }
    )
    ctx = HostContext(runner=runner, files=fake_context().files)
    facts = gpu_facts(ctx)
    assert facts["gpu.0.throttle_reasons"].value == "hw_thermal_slowdown"
    ops = [call[0] for call in runner.calls]
    assert ops.index("gpu.throttle_event") < ops.index("gpu.throttle_legacy")


def test_mixed_models_are_reported() -> None:
    csv_text = (
        "0, NVIDIA GeForce RTX 4090, 24564, 30, 50.0, 200, 10000, [N/A], 4, 16\n"
        "1, NVIDIA GeForce RTX 3090, 24576, 30, 50.0, 200, 9000, [N/A], 4, 16\n"
    )
    ctx = fake_context(
        results={
            "gpu.query": ok("gpu.query", csv_text),
            "gpu.throttle_event": failed("gpu.throttle_event"),
            "gpu.throttle_legacy": failed("gpu.throttle_legacy"),
            "gpu.lspci_nvidia": ok("gpu.lspci_nvidia", ""),
        }
    )
    facts = gpu_facts(ctx)
    assert facts["gpu.models_identical"].value is False
    assert facts["gpu.vram_min_mib"].value == 24564


# -- driver edge cases ----------------------------------------------------------


def smi_ctx(text: str) -> HostContext:
    return fake_context(results={"driver.smi_summary": ok("driver.smi_summary", text)})


def test_cuda_version_not_available_is_unsupported() -> None:
    facts = driver_facts(smi_ctx("Driver Version: 595.91.07   CUDA Version: N/A"))
    assert facts["driver.version"].value == "595.91.07"
    assert facts["driver.cuda_version"].status is FactStatus.UNSUPPORTED
    assert "driver.cuda_version_code" not in facts


def test_output_without_versions_is_an_error() -> None:
    facts = driver_facts(smi_ctx("something unexpected"))
    assert facts["driver.nvidia_smi_responding"].value is True
    assert facts["driver.version"].status is FactStatus.ERROR
    assert facts["driver.cuda_version"].status is FactStatus.ERROR


# -- small parsers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "value", "status"),
    [
        ("34", "int", 34, FactStatus.OK),
        (" 54.20 ", "float", 54.2, FactStatus.OK),
        ("Enabled", "str", "Enabled", FactStatus.OK),
        ("[N/A]", "int", None, FactStatus.UNSUPPORTED),
        ("[Not Supported]", "str", None, FactStatus.UNSUPPORTED),
        ("ERR!", "int", None, FactStatus.ERROR),
        ("[Unknown Error]", "int", None, FactStatus.ERROR),
        ("[GPU is lost]", "int", None, FactStatus.ERROR),
        ("abc", "int", None, FactStatus.ERROR),
    ],
)
def test_parse_field(raw: str, kind: str, value: object, status: FactStatus) -> None:
    parsed, parsed_status, _ = parse_field(raw, kind)
    assert (parsed, parsed_status) == (value, status)


@pytest.mark.parametrize(
    ("hex_text", "expected"),
    [
        ("0x0000000000000000", "none"),
        ("0x1", "gpu_idle"),
        ("0x0000000000000028", "hw_slowdown,sw_thermal_slowdown"),
        ("0xC0", "hw_thermal_slowdown,hw_power_brake_slowdown"),
    ],
)
def test_decode_throttle(hex_text: str, expected: str) -> None:
    assert decode_throttle(hex_text) == (expected, None)


def test_decode_throttle_rejects_garbage() -> None:
    names, problem = decode_throttle("not hex")
    assert names == ""
    assert problem


@pytest.mark.parametrize(
    ("version", "code"),
    [("11.8", 11080), ("12.6", 12060), ("12.10", 12100), ("N/A", None), ("", None)],
)
def test_cuda_code(version: str, code: int | None) -> None:
    assert cuda_code(version) == code


def test_cuda_below_11_8_is_comparable_by_code() -> None:
    assert cuda_code("11.7") < cuda_code("11.8") < cuda_code("12.0")  # type: ignore[operator]


def test_registry_includes_gpu_and_driver() -> None:
    assert {"system", "gpu", "driver"} <= set(COLLECTORS)
