import json
from pathlib import Path

import pytest

from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.disk import (
    DiskCollector,
    parse_ata_attributes,
    parse_df,
    parse_lsblk,
    parse_nvme,
    smart_health,
)
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import CommandResult, CommandStatus, SubprocessRunner
from vast_troubleshooter.testing import FakeRunner, fake_context

FIXTURES = Path(__file__).parent / "fixtures"
DISK = FIXTURES / "disk"


def disk(name: str) -> str:
    return (DISK / name).read_text()


def ok(op: str, body: str) -> CommandResult:
    return CommandResult(op, CommandStatus.OK, returncode=0, stdout=body)


def smart_ok(body: str, code: int = 0) -> CommandResult:
    # smartctl's exit code is a bitmask; non-zero is common and not a failure.
    status = CommandStatus.OK if code == 0 else CommandStatus.ERROR
    return CommandResult("disk.smart_x", status, returncode=code, stdout=body)


def results(
    lsblk: str = "lsblk_nvme.txt",
    docker: str = "df_docker_own.txt",
    smart: dict[str, CommandResult] | None = None,
) -> dict[str, CommandResult]:
    out = {
        "disk.df[path=/]": ok("disk.df", disk("df_root.txt")),
        "disk.df[path=/var/lib/docker]": ok("disk.df", disk(docker)),
        "disk.findmnt_root": ok("disk.findmnt_root", disk("findmnt_rw.txt")),
        "disk.lsblk": ok("disk.lsblk", disk(lsblk)),
        "docker.info": ok(
            "docker.info", (FIXTURES / "docker" / "running_nvidia.json").read_text()
        ),
    }
    for device, result in (smart or {}).items():
        out[f"disk.smart_x[device=/dev/{device}]"] = result
    return out


def collect(res: dict[str, CommandResult]) -> tuple[dict[str, Fact], FakeRunner]:
    runner = FakeRunner(res)
    ctx = HostContext(runner=runner, files=fake_context().files)
    return {f.key: f for f in DiskCollector().collect(ctx)}, runner


# -- space -----------------------------------------------------------------------


def test_root_and_docker_space_on_a_dedicated_drive() -> None:
    facts, _ = collect(results())
    assert facts["disk.root_total_bytes"].value == 982_000_000_000
    assert facts["disk.root_free_bytes"].value == 812_000_000_000
    assert facts["disk.root_free_bytes"].unit == "B"
    assert facts["disk.docker_dir_total_bytes"].value == 2_000_000_000_000
    assert facts["disk.docker_dir_free_bytes"].value == 1_700_000_000_000
    assert facts["disk.docker_shares_root_fs"].value is False


def test_docker_on_the_root_filesystem_is_detected() -> None:
    facts, _ = collect(results(docker="df_docker_same.txt"))
    assert facts["disk.docker_shares_root_fs"].value is True


def test_docker_dir_unknown_when_docker_is_unavailable() -> None:
    res = results()
    res["docker.info"] = CommandResult("docker.info", CommandStatus.MISSING)
    facts, runner = collect(res)
    for key in (
        "disk.docker_dir_total_bytes",
        "disk.docker_dir_free_bytes",
        "disk.docker_shares_root_fs",
    ):
        assert facts[key].status is FactStatus.MISSING
    assert facts["disk.root_free_bytes"].status is FactStatus.OK
    assert not [c for c in runner.calls if c[0] == "disk.df" and c[1]["path"] != "/"]


@pytest.mark.parametrize(
    "root", ["", "relative/path", "/var/../etc", "/var/lib/do cker", "/x;y"]
)
def test_untrusted_docker_dir_is_never_passed_to_df(root: str) -> None:
    res = results()
    res["docker.info"] = ok("docker.info", json.dumps({"DockerRootDir": root}))
    facts, runner = collect(res)
    assert facts["disk.docker_dir_total_bytes"].status is FactStatus.MISSING
    assert [c[1]["path"] for c in runner.calls if c[0] == "disk.df"] == ["/"]


def test_df_failure_and_unparseable_output() -> None:
    res = results()
    res["disk.df[path=/]"] = CommandResult("disk.df", CommandStatus.TIMEOUT)
    facts, _ = collect(res)
    assert facts["disk.root_free_bytes"].status is FactStatus.TIMEOUT
    res["disk.df[path=/]"] = ok("disk.df", "garbage")
    facts, _ = collect(res)
    assert facts["disk.root_free_bytes"].status is FactStatus.ERROR


def test_root_mount_options() -> None:
    facts, _ = collect(results())
    assert facts["disk.root_mounted_rw"].value is True
    res = results()
    res["disk.findmnt_root"] = ok("disk.findmnt_root", disk("findmnt_ro.txt"))
    facts, _ = collect(res)
    assert facts["disk.root_mounted_rw"].value is False


# -- drives ----------------------------------------------------------------------


def test_drive_types_from_lsblk() -> None:
    facts, _ = collect(results(lsblk="lsblk_mixed.txt"))
    assert facts["disk.drive_count"].value == 5
    assert facts["disk.hdd_count"].value == 2  # sdb and the virtual disk
    assert facts["disk.nvme0n1.rotational"].value is False
    assert facts["disk.nvme0n1.transport"].value == "nvme"
    assert facts["disk.sdb.rotational"].value is True
    assert facts["disk.sdb.size_bytes"].value == 4_000_787_030_016
    assert facts["disk.sda.model"].value == "Samsung SSD 860 EVO 500GB"
    assert "disk.vda.transport" not in facts  # no transport reported


def test_smart_is_read_only_for_physical_drives_from_the_enumeration() -> None:
    smart = {n: smart_ok(disk("smart_sata_healthy.txt")) for n in ("sda", "sdb")}
    smart["nvme0n1"] = smart_ok(disk("smart_nvme_healthy.txt"))
    _, runner = collect(results(lsblk="lsblk_mixed.txt", smart=smart))
    devices = [c[1]["device"] for c in runner.calls if c[0] == "disk.smart_x"]
    assert devices == ["/dev/nvme0n1", "/dev/sda", "/dev/sdb"]  # no usb, no vda


def test_bad_drive_name_is_an_error_row_and_never_reaches_smartctl() -> None:
    facts, runner = collect(results(lsblk="lsblk_bad_name.txt"))
    assert facts["disk.row"].status is FactStatus.ERROR
    assert facts["disk.drive_count"].value == 0
    assert not [c for c in runner.calls if c[0] == "disk.smart_x"]


def test_lsblk_failure() -> None:
    res = results()
    res["disk.lsblk"] = CommandResult("disk.lsblk", CommandStatus.MISSING)
    facts, _ = collect(res)
    assert facts["disk.drive_count"].status is FactStatus.MISSING
    assert "disk.smart_failed_count" not in facts


# -- SMART: NVMe -----------------------------------------------------------------


def test_healthy_nvme() -> None:
    facts, _ = collect(
        results(smart={"nvme0n1": smart_ok(disk("smart_nvme_healthy.txt"))})
    )
    assert facts["disk.nvme0n1.smart_passed"].value is True
    assert facts["disk.nvme0n1.critical_warning"].value == "0x00"
    assert facts["disk.nvme0n1.available_spare_pct"].value == 100
    assert facts["disk.nvme0n1.spare_threshold_pct"].value == 10
    assert facts["disk.nvme0n1.percentage_used"].value == 1
    assert facts["disk.nvme0n1.media_errors"].value == 0
    assert facts["disk.nvme0n1.spare_below_threshold"].value is False
    assert facts["disk.smart_failed_count"].value == 0
    assert facts["disk.nvme_critical_warning_count"].value == 0
    assert facts["disk.nvme_media_errors_total"].value == 0


def test_failing_nvme_is_read_even_though_smartctl_exits_non_zero() -> None:
    facts, _ = collect(
        results(smart={"nvme0n1": smart_ok(disk("smart_nvme_failing.txt"), code=8)})
    )
    assert facts["disk.nvme0n1.smart_passed"].value is False
    assert facts["disk.nvme0n1.critical_warning"].value == "0x01"
    assert facts["disk.nvme0n1.media_errors"].value == 1204  # comma stripped
    assert facts["disk.nvme0n1.spare_below_threshold"].value is True
    assert facts["disk.smart_failed_count"].value == 1
    assert facts["disk.nvme_critical_warning_count"].value == 1
    assert facts["disk.nvme_media_errors_total"].value == 1204


# -- SMART: SATA -----------------------------------------------------------------


def test_healthy_sata() -> None:
    facts, _ = collect(
        results(
            lsblk="lsblk_mixed.txt",
            smart={"sda": smart_ok(disk("smart_sata_healthy.txt"))},
        )
    )
    assert facts["disk.sda.smart_passed"].value is True
    assert facts["disk.sda.reallocated_sectors"].value == 0
    assert facts["disk.sda.pending_sectors"].value == 0
    assert facts["disk.sda.reported_uncorrect"].value == 0
    assert facts["disk.sda.crc_errors"].value == 0


def test_failing_sata_with_pending_sectors() -> None:
    smart = {"sda": smart_ok(disk("smart_sata_failing.txt"), code=8)}
    facts, _ = collect(results(lsblk="lsblk_mixed.txt", smart=smart))
    assert facts["disk.sda.smart_passed"].value is False
    assert facts["disk.sda.reallocated_sectors"].value == 24
    assert facts["disk.sda.pending_sectors"].value == 8
    assert facts["disk.sda.offline_uncorrectable"].value == 2
    assert facts["disk.sda.reported_uncorrect"].value == 3
    assert facts["disk.sata_reallocated_total"].value == 24
    assert facts["disk.sata_pending_total"].value == 8
    assert facts["disk.sata_uncorrectable_total"].value == 5  # 3 reported + 2 offline


def test_hdd_is_flagged_and_its_smart_is_read() -> None:
    facts, _ = collect(
        results(lsblk="lsblk_mixed.txt", smart={"sdb": smart_ok(disk("smart_hdd.txt"))})
    )
    assert facts["disk.sdb.rotational"].value is True
    assert facts["disk.sdb.smart_passed"].value is True
    assert facts["disk.hdd_count"].value == 2


def test_totals_add_up_across_drives() -> None:
    smart = {
        "sda": smart_ok(disk("smart_sata_failing.txt"), code=8),
        "sdb": smart_ok(disk("smart_sata_failing.txt"), code=8),
        "nvme0n1": smart_ok(disk("smart_nvme_healthy.txt")),
    }
    facts, _ = collect(results(lsblk="lsblk_mixed.txt", smart=smart))
    assert facts["disk.sata_pending_total"].value == 16
    assert facts["disk.smart_failed_count"].value == 2


# -- unreadable SMART ------------------------------------------------------------


def test_smart_needs_root_without_hiding_the_other_facts() -> None:
    root = CommandResult(
        "disk.smart_x",
        CommandStatus.NEEDS_ROOT,
        detail="needs root; elevated reads are not enabled",
    )
    facts, _ = collect(results(smart={"nvme0n1": root}))
    assert facts["disk.nvme0n1.smart_passed"].status is FactStatus.NEEDS_ROOT
    assert facts["disk.nvme0n1.smart_passed"].value is None
    assert facts["disk.root_free_bytes"].status is FactStatus.OK
    assert facts["disk.drive_count"].value == 1


def test_unreadable_smart_never_looks_healthy() -> None:
    facts, _ = collect(
        results(
            smart={"nvme0n1": CommandResult("disk.smart_x", CommandStatus.NEEDS_ROOT)}
        )
    )
    total = facts["disk.smart_failed_count"]  # present, but never a usable zero
    assert total.status is not FactStatus.OK and total.value is None
    assert not any(
        k.endswith("smart_passed") and f.value is True for k, f in facts.items()
    )


def test_smartctl_error_without_smart_output_is_an_error() -> None:
    bad = CommandResult(
        "disk.smart_x",
        CommandStatus.ERROR,
        returncode=2,
        stdout="Smartctl open device: /dev/sda failed: Permission denied",
    )
    facts, _ = collect(results(smart={"nvme0n1": bad}))
    assert facts["disk.nvme0n1.smart_passed"].status is FactStatus.ERROR


def test_drive_without_a_health_line_is_unsupported() -> None:
    facts, _ = collect(
        results(
            smart={
                "nvme0n1": smart_ok(
                    "SMART support is: Unavailable - device lacks SMART capability.\n"
                )
            }
        )
    )
    assert facts["disk.nvme0n1.smart_passed"].status is FactStatus.UNSUPPORTED


def test_sas_health_status_line() -> None:
    assert smart_health("SMART Health Status: OK\n") is True
    assert (
        smart_health("SMART Health Status: FAILURE PREDICTION THRESHOLD EXCEEDED\n")
        is False
    )


# -- nothing identifying, nothing written ----------------------------------------


def test_serial_numbers_never_reach_a_fact() -> None:
    smart = {
        "nvme0n1": smart_ok(disk("smart_nvme_healthy.txt")),
        "sda": smart_ok(disk("smart_sata_healthy.txt")),
    }
    facts, _ = collect(results(lsblk="lsblk_mixed.txt", smart=smart))
    dumped = " ".join(f.model_dump_json() for f in facts.values())
    assert "S5GXNX0T123456A" not in dumped
    assert "WD-WCC4N1234567" not in dumped


def test_facts_are_safe_to_share() -> None:
    smart = {
        "nvme0n1": smart_ok(disk("smart_nvme_failing.txt"), code=8),
        "sda": smart_ok(disk("smart_sata_failing.txt"), code=8),
    }
    facts, _ = collect(results(lsblk="lsblk_mixed.txt", smart=smart))
    for fact in facts.values():
        fact.to_shareable()


def test_no_operation_can_start_a_smart_self_test() -> None:
    op = READ_OPERATIONS["disk.smart_x"]
    assert op.args == ("-x", "{device}")
    for registered in READ_OPERATIONS.values():
        if registered.executable == "smartctl":
            assert "-t" not in registered.args and "--test" not in registered.args


def test_smart_device_operand_only_accepts_physical_drive_paths() -> None:
    runner = SubprocessRunner(
        READ_OPERATIONS,
        is_root=lambda: True,
        which=lambda name, path: f"/usr/bin/{name}",
    )
    argv, early = runner.plan("disk.smart_x", device="/dev/nvme0n1")
    assert early is None and argv[-2:] == ["-x", "/dev/nvme0n1"]
    for bad in (
        "/dev/sda; reboot",
        "/dev/loop0",
        "sda",
        "/etc/passwd",
        "-t",
        "/dev/sd",
    ):
        assert runner.plan("disk.smart_x", device=bad)[1] is not None


def test_df_operand_only_accepts_plain_absolute_paths() -> None:
    runner = SubprocessRunner(
        READ_OPERATIONS, which=lambda name, path: f"/usr/bin/{name}"
    )
    assert runner.plan("disk.df", path="/var/lib/docker")[1] is None
    for bad in ("relative", "/a b", "/x;y", "$HOME", "-h"):
        assert runner.plan("disk.df", path=bad)[1] is not None


# -- parsers ---------------------------------------------------------------------


def test_parse_lsblk_decodes_escaped_spaces() -> None:
    rows = parse_lsblk('NAME="sda" MODEL="My\\x20Disk" TRAN="sata"\n')
    assert rows == [{"NAME": "sda", "MODEL": "My Disk", "TRAN": "sata"}]


def test_parse_df_variants() -> None:
    assert parse_df(disk("df_root.txt")) == (
        "/dev/nvme0n1p2",
        982_000_000_000,
        812_000_000_000,
        "/",
    )
    assert parse_df("only a header\n") is None
    assert parse_df("h\nfs notnumber 1 2 3 /\n") is None


def test_parse_nvme_handles_commas_and_percent_signs() -> None:
    found = parse_nvme(disk("smart_nvme_failing.txt"))
    assert found["media_errors"] == 1204
    assert found["available_spare_pct"] == 5
    assert found["spare_threshold_pct"] == 10  # not confused with "Available Spare"


def test_parse_ata_attributes_ignores_other_ids_and_trailing_notes() -> None:
    found = parse_ata_attributes(disk("smart_sata_failing.txt"))
    assert found == {
        "reallocated_sectors": 24,
        "reported_uncorrect": 3,
        "pending_sectors": 8,
        "offline_uncorrectable": 2,
        "crc_errors": 0,
    }


def test_registry_includes_disk() -> None:
    assert "disk" in COLLECTORS


# -- a total is only a zero if every applicable drive was fully read (F-01) ----------------------
# Found by independent testing: a readable SATA drive let the NVMe rules PASS while the NVMe
# drive's SMART query had timed out.

from vast_troubleshooter.rule_engine import evaluate_rules, load_default_rules

_RULES = {r.id: r for r in load_default_rules()}
_MIXED = (
    'NAME="nvme0n1" ROTA="0" SIZE="1000204886016" MODEL="X" TRAN="nvme"\n'
    'NAME="sda" ROTA="0" SIZE="500107862016" MODEL="Y" TRAN="sata"\n'
)


def _statuses(
    lsblk: str, smart: dict[str, CommandResult]
) -> tuple[dict[str, Fact], dict[str, str]]:
    res = results()
    res["disk.lsblk"] = ok("disk.lsblk", lsblk)
    for device, result in smart.items():
        res[f"disk.smart_x[device=/dev/{device}]"] = result
    facts, _ = collect(res)
    ruled = {
        r.rule_id: r.status.value
        for r in evaluate_rules(list(_RULES.values()), list(facts.values()))
    }
    return facts, ruled


def test_a_readable_sata_drive_cannot_vouch_for_an_nvme_drive_that_timed_out() -> None:
    facts, rules = _statuses(
        _MIXED,
        {
            "sda": smart_ok(disk("smart_sata_healthy.txt")),
            "nvme0n1": CommandResult("disk.smart_x", CommandStatus.TIMEOUT),
        },
    )
    assert facts["disk.nvme_media_errors_total"].status is FactStatus.ERROR
    assert facts["disk.nvme_media_errors_total"].value is None
    assert "could not be fully read" in (
        facts["disk.nvme_media_errors_total"].raw_excerpt or ""
    )
    assert rules["disk.nvme_unhealthy"] == "unknown"  # was "pass"
    assert (
        rules["disk.smart_health_failed"] == "unknown"
    )  # one drive's health is not known
    # The SATA drive was fully read, so the SATA totals are real zeros.
    assert facts["disk.sata_pending_total"].value == 0
    assert rules["disk.sata_bad_sectors"] == "pass"


def test_a_failure_on_any_drive_stands_even_if_another_drive_could_not_be_read() -> (
    None
):
    facts, rules = _statuses(
        _MIXED,
        {
            "sda": smart_ok(disk("smart_sata_failing.txt"), code=8),
            "nvme0n1": CommandResult("disk.smart_x", CommandStatus.TIMEOUT),
        },
    )
    assert (
        facts["disk.smart_failed_count"].value == 1
    )  # positive evidence is never cancelled
    assert facts["disk.sata_pending_total"].value == 8
    assert rules["disk.smart_health_failed"] == "fail"
    assert rules["disk.sata_bad_sectors"] == "fail"
    assert rules["disk.nvme_unhealthy"] == "unknown"


def test_an_nvme_failure_stands_even_if_a_sata_drive_could_not_be_read() -> None:
    _, rules = _statuses(
        _MIXED,
        {
            "nvme0n1": smart_ok(disk("smart_nvme_failing.txt"), code=8),
            "sda": CommandResult("disk.smart_x", CommandStatus.NEEDS_ROOT),
        },
    )
    assert rules["disk.nvme_unhealthy"] == "fail"
    assert rules["disk.smart_health_failed"] == "fail"
    assert rules["disk.sata_bad_sectors"] == "unknown"


def test_all_drives_fully_read_gives_real_zeros_and_passes() -> None:
    facts, rules = _statuses(
        _MIXED,
        {
            "sda": smart_ok(disk("smart_sata_healthy.txt")),
            "nvme0n1": smart_ok(disk("smart_nvme_healthy.txt")),
        },
    )
    for key in (
        "smart_failed_count",
        "nvme_critical_warning_count",
        "nvme_media_errors_total",
        "sata_pending_total",
    ):
        assert (
            facts[f"disk.{key}"].value == 0
            and facts[f"disk.{key}"].status is FactStatus.OK
        )
    assert {
        rules[r]
        for r in (
            "disk.smart_health_failed",
            "disk.nvme_unhealthy",
            "disk.sata_bad_sectors",
        )
    } == {"pass"}


def test_a_drive_with_no_health_line_makes_the_health_total_unknown() -> None:
    _, rules = _statuses(
        _MIXED,
        {
            "sda": smart_ok(disk("smart_sata_healthy.txt")),
            "nvme0n1": smart_ok(
                "SMART support is: Unavailable - device lacks SMART capability.\n"
            ),
        },
    )
    assert rules["disk.smart_health_failed"] == "unknown"
    assert rules["disk.nvme_unhealthy"] == "unknown"  # no NVMe health fields either


def test_an_nvme_drive_read_without_its_nvme_fields_is_incomplete() -> None:
    health_only = "SMART overall-health self-assessment test result: PASSED\n"
    facts, rules = _statuses(
        _MIXED.splitlines()[0] + "\n", {"nvme0n1": smart_ok(health_only)}
    )
    assert facts["disk.nvme_media_errors_total"].status is FactStatus.ERROR
    assert rules["disk.nvme_unhealthy"] == "unknown"


def test_a_sas_style_drive_without_an_attribute_table_leaves_the_sata_totals_unknown() -> (
    None
):
    sas = 'NAME="sda" ROTA="1" SIZE="1" MODEL="S" TRAN="sas"\n'
    _, rules = _statuses(sas, {"sda": smart_ok("SMART Health Status: OK\n")})
    assert rules["disk.smart_health_failed"] == "pass"  # its health WAS read
    assert rules["disk.sata_bad_sectors"] == "unknown"  # but no sector counts were


def test_only_nvme_drives_present_means_no_sata_drive_is_missing() -> None:
    facts, rules = _statuses(
        _MIXED.splitlines()[0] + "\n",
        {"nvme0n1": smart_ok(disk("smart_nvme_healthy.txt"))},
    )
    assert facts["disk.sata_pending_total"].value == 0  # there is nothing SATA to read
    assert rules["disk.sata_bad_sectors"] == "pass"


def test_no_physical_drives_means_no_totals_at_all() -> None:
    facts, rules = _statuses('NAME="vda" ROTA="1" SIZE="1" MODEL="" TRAN=""\n', {})
    assert "disk.smart_failed_count" not in facts
    assert rules["disk.smart_health_failed"] == "unknown"
