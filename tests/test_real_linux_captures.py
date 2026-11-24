"""Collectors against REAL tool output, captured 2026-11-23 inside an Ubuntu 24.04
container (arm64) on Docker Desktop's WSL2 kernel.

This is real Linux output, but it is a container, not a Vast host: no GPUs, no
systemd, no Docker socket, virtual disks. See tests/fixtures/README.md.
"""

from pathlib import Path

from vast_troubleshooter.collectors.disk import DiskCollector, parse_df, parse_lsblk
from vast_troubleshooter.collectors.gpu import GpuCollector
from vast_troubleshooter.collectors.kernel_log import KernelLogCollector
from vast_troubleshooter.collectors.network import NetworkCollector, parse_nameservers
from vast_troubleshooter.collectors.system import SystemCollector
from vast_troubleshooter.collectors.vast_host import VastHostCollector
from vast_troubleshooter.fixes.definitions import _default_link
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeNetwork, fake_context

REAL = Path(__file__).parent / "fixtures" / "real_linux_container"


def text(name: str) -> str:
    return (REAL / name).read_text()


def ok(op: str, name: str) -> CommandResult:
    return CommandResult(op, CommandStatus.OK, returncode=0, stdout=text(name))


def by_key(facts: list[Fact]) -> dict[str, Fact]:
    return {f.key: f for f in facts}


def test_system_facts_from_a_real_ubuntu_container() -> None:
    ctx = fake_context(
        results={"system.uname_release": ok("system.uname_release", "uname_r.txt")},
        files={
            "/etc/os-release": text("os-release"),
            "/proc/uptime": text("proc_uptime.txt"),
            "/proc/version": text("proc_version.txt"),
            "/.dockerenv": "",  # present in the container the captures came from
        },
    )
    facts = by_key(SystemCollector().collect(ctx))
    assert facts["system.os_id"].value == "ubuntu"
    assert facts["system.os_version_id"].value == "24.04"
    assert facts["system.os_pretty_name"].value == "Ubuntu 24.04.5 LTS"
    assert "microsoft" in str(facts["system.kernel_release"].value).lower()
    assert isinstance(facts["system.uptime_seconds"].value, float)
    assert facts["system.on_wsl_kernel"].value is True
    assert facts["system.environment"].value == "container on a WSL2 kernel"
    assert all(f.status is FactStatus.OK for f in facts.values())


def test_a_real_docker_generated_resolv_conf() -> None:
    assert parse_nameservers(text("resolv.conf")) == ["192.168.65.7"]
    ctx = fake_context(
        files={"/etc/resolv.conf": text("resolv.conf")}, network=FakeNetwork()
    )
    facts = by_key(NetworkCollector().collect(ctx))
    assert facts["network.nameserver_count"].value == 1
    assert facts["network.nameserver_loopback_count"].value == 0
    assert facts["network.nameserver_public_count"].value == 0  # a private address


def real_disk_results() -> dict[str, CommandResult]:
    return {
        "disk.lsblk": ok("disk.lsblk", "lsblk.txt"),
        "disk.df[path=/]": ok("disk.df", "df_root.txt"),
        "disk.findmnt_root": ok("disk.findmnt_root", "findmnt_root.txt"),
        "docker.info": CommandResult("docker.info", CommandStatus.MISSING),
    }


def test_real_lsblk_output_including_padded_model_names() -> None:
    rows = parse_lsblk(text("lsblk.txt"))
    assert len(rows) == 5
    assert (
        rows[0]["MODEL"] == "Virtual Disk    "
    )  # real lsblk pads the model with spaces
    assert rows[0]["TRAN"] == ""


def test_real_disk_facts() -> None:
    facts = by_key(DiskCollector().collect(fake_context(results=real_disk_results())))
    assert facts["disk.drive_count"].value == 5
    assert facts["disk.sda.model"].value == "Virtual Disk"  # padding stripped
    assert facts["disk.root_total_bytes"].value > 10**11
    assert facts["disk.root_free_bytes"].value > 0
    assert facts["disk.docker_dir_total_bytes"].status is FactStatus.MISSING
    # No transport is reported for virtual disks, so no SMART call is ever made.
    assert not any(k.endswith("smart_passed") for k in facts)


def test_virtual_disks_report_rotational_so_hdd_detection_must_not_trust_it_alone() -> (
    None
):
    """A real observation: every virtual disk says ROTA=1. A future 'Docker needs an
    SSD' rule must look at more than this flag (BACKLOG)."""
    facts = by_key(DiskCollector().collect(fake_context(results=real_disk_results())))
    assert facts["disk.hdd_count"].value == 5
    assert facts["disk.sda.rotational"].value is True


def test_a_real_overlay_root_with_a_very_long_options_string_still_reads_as_writable() -> (
    None
):
    assert len(text("findmnt_root.txt")) > 400
    facts = by_key(DiskCollector().collect(fake_context(results=real_disk_results())))
    assert facts["disk.root_mounted_rw"].value is True


def test_real_df_output() -> None:
    parsed = parse_df(text("df_root.txt"))
    assert parsed is not None
    filesystem, total, available, mount = parsed
    assert filesystem == "overlay" and mount == "/" and total > available > 0


def test_a_real_lspci_with_no_nvidia_hardware_is_a_count_of_zero() -> None:
    assert text("lspci_nvidia.txt") == ""
    ctx = fake_context(
        results={"gpu.lspci_nvidia": ok("gpu.lspci_nvidia", "lspci_nvidia.txt")}
    )
    assert by_key(GpuCollector().collect(ctx))["gpu.pci_count"].value == 0


def test_the_real_default_route_line_gives_the_link() -> None:
    ctx = fake_context(
        results={
            "dns.default_route": ok("dns.default_route", "ip_route_default.txt"),
            "dns.link_servers": "Link 2 (eth0):\n",
            "dns.link_domains": "Link 2 (eth0):\n",
        }
    )
    assert dict(_default_link(ctx) or {}) == {
        "link": "eth0"
    }  # despite the trailing space


def test_the_real_missing_daemon_log_message_is_a_missing_log_not_an_error() -> None:
    failed = CommandResult(
        "vast.daemon_log_tail",
        CommandStatus.ERROR,
        returncode=1,
        stderr=text("tail_missing_log.txt"),
    )
    ctx = fake_context(results={"vast.daemon_log_tail": failed})
    facts = by_key(VastHostCollector().collect(ctx))
    assert facts["vast_host.log_lines_read"].status is FactStatus.MISSING


def test_the_real_dmesg_permission_error_never_looks_like_a_clean_log() -> None:
    denied = CommandResult(
        "kernel.dmesg",
        CommandStatus.ERROR,
        returncode=1,
        stderr=text("dmesg_nonroot.txt"),
    )
    gone = CommandResult("kernel.journal", CommandStatus.MISSING)
    ctx = fake_context(results={"kernel.journal": gone, "kernel.dmesg": denied})
    facts = by_key(KernelLogCollector().collect(ctx))
    assert facts["kernel_log.xid_count"].status is FactStatus.ERROR
    assert facts["kernel_log.xid_count"].value is None
