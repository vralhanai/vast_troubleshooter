"""Storage facts from the Vast SSD health page. Read only; no self-tests.

Space comes from `df`, drive type from `lsblk`, health from `smartctl -x`.
smartctl exits non-zero for many non-fatal reasons (its exit code is a
bitmask, and 8 means "SMART status FAILED"), so its output is parsed even when
the exit code is not zero. Serial numbers and other identifiers are never kept.
"""

import json
import re
from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus

_NAME = re.compile(r"[A-Za-z0-9_-]+")
_PHYSICAL_TRANSPORTS = {"nvme", "sata", "sas", "ata", "scsi"}
_DEVICE_NAME = re.compile(r"nvme\d+n\d+|sd[a-z]{1,3}")
_PAIR = re.compile(r'(\w+)="([^"]*)"')
# Whole word, case-sensitive: smartctl's own errors say "Smartctl", not "SMART".
_SMART_WORD = re.compile(r"\bSMART\b")
_HEALTH = re.compile(
    r"overall-health self-assessment test result:\s*(\w+)", re.IGNORECASE
)
_SAS_HEALTH = re.compile(r"SMART Health Status:\s*(\w+)", re.IGNORECASE)
_ATTR_IDS = {
    5: "reallocated_sectors",
    187: "reported_uncorrect",
    197: "pending_sectors",
    198: "offline_uncorrectable",
    199: "crc_errors",
}


def parse_lsblk(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        pairs = {k: _unescape(v) for k, v in _PAIR.findall(line)}
        if pairs:
            rows.append(pairs)
    return rows


def _unescape(value: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)


def parse_df(text: str) -> tuple[str, int, int, str] | None:
    """(filesystem, total bytes, available bytes, mount point) or None."""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None
    parts = lines[-1].split(None, 5)
    if len(parts) < 6:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[3]), parts[5].strip()
    except ValueError:
        return None


def _int(text: str) -> int | None:
    try:
        return int(text.replace(",", "").strip().split()[0])
    except ValueError, IndexError:
        return None


def parse_nvme(text: str) -> dict[str, int | str]:
    found: dict[str, int | str] = {}
    fields = {
        "Critical Warning": "critical_warning",
        "Available Spare Threshold": "spare_threshold_pct",
        "Available Spare": "available_spare_pct",
        "Percentage Used": "percentage_used",
        "Media and Data Integrity Errors": "media_errors",
    }
    for line in text.splitlines():
        label, sep, rest = line.partition(":")
        key = fields.get(label.strip())
        if not sep or key is None:
            continue
        rest = rest.strip().rstrip("%").strip()
        if key == "critical_warning":
            found[key] = rest.split()[0] if rest else ""
        elif (number := _int(rest)) is not None:
            found[key] = number
    return found


def parse_ata_attributes(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for line in text.splitlines():
        stripped = re.sub(r"\s*\(.*\)\s*$", "", line).strip()
        tokens = stripped.split()
        if len(tokens) < 8 or not tokens[0].isdigit():
            continue
        name = _ATTR_IDS.get(int(tokens[0]))
        raw = _int(tokens[-1])
        if name and raw is not None:
            found[name] = raw
    return found


def smart_health(text: str) -> bool | None:
    match = _HEALTH.search(text)
    if match:
        return match.group(1).upper() == "PASSED"
    sas = _SAS_HEALTH.search(text)
    if sas:
        return sas.group(1).upper() == "OK"
    return None


class DiskCollector(Collector):
    name: ClassVar[str] = "disk"

    def collect(self, ctx: HostContext) -> list[Fact]:
        facts = self._space(ctx)
        facts += self._root_mount(ctx)
        facts += self._drives(ctx)
        return facts

    # -- space ----------------------------------------------------------------

    def _df(
        self, ctx: HostContext, path: str
    ) -> tuple[tuple[str, int, int, str] | None, CommandResult]:
        result = ctx.runner.run("disk.df", path=path)
        return (parse_df(result.stdout) if result.ok else None), result

    def _space(self, ctx: HostContext) -> list[Fact]:
        facts: list[Fact] = []
        root, result = self._df(ctx, "/")
        if root is None:
            facts += self._df_failure(
                ("disk.root_total_bytes", "disk.root_free_bytes"), result
            )
        else:
            src = result.source_command
            facts += [
                self.fact(
                    "disk.root_total_bytes", root[1], unit="B", source_command=src
                ),
                self.fact(
                    "disk.root_free_bytes", root[2], unit="B", source_command=src
                ),
            ]

        keys = (
            "disk.docker_dir_total_bytes",
            "disk.docker_dir_free_bytes",
            "disk.docker_shares_root_fs",
        )
        docker_dir, why = self._docker_dir(ctx)
        if docker_dir is None:
            facts += [
                self.fact(k, status=FactStatus.MISSING, raw_excerpt=why) for k in keys
            ]
            return facts
        docker, result = self._df(ctx, docker_dir)
        if docker is None:
            return facts + self._df_failure(keys, result)
        src = result.source_command
        facts += [
            self.fact(keys[0], docker[1], unit="B", source_command=src),
            self.fact(keys[1], docker[2], unit="B", source_command=src),
        ]
        if root is not None:
            same = (docker[0], docker[3]) == (root[0], root[3])
            facts.append(self.fact(keys[2], same, source_command=src))
        else:
            facts.append(
                self.fact(
                    keys[2],
                    status=FactStatus.ERROR,
                    raw_excerpt="root filesystem unknown",
                )
            )
        return facts

    def _df_failure(self, keys: tuple[str, ...], result: CommandResult) -> list[Fact]:
        if result.ok:  # df ran but its output could not be parsed
            return [
                self.fact(
                    k, status=FactStatus.ERROR, raw_excerpt="unparseable df output"
                )
                for k in keys
            ]
        return [self.failed_from_result(k, result) for k in keys]

    def _docker_dir(self, ctx: HostContext) -> tuple[str | None, str]:
        result = ctx.runner.run("docker.info")
        if not result.ok:
            return None, f"docker info unavailable ({result.status.value})"
        try:
            info = json.loads(result.stdout)
        except ValueError:
            return None, "unparseable docker info output"
        root = info.get("DockerRootDir") if isinstance(info, dict) else None
        if not isinstance(root, str) or not root.startswith("/") or ".." in root:
            return None, "Docker root dir unknown"
        if not re.fullmatch(r"/[A-Za-z0-9_./+-]*", root):
            return None, "Docker root dir has unexpected characters"
        return root, ""

    def _root_mount(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("disk.findmnt_root")
        key = "disk.root_mounted_rw"
        if not result.ok:
            return [self.failed_from_result(key, result)]
        options = {o.strip() for o in result.stdout.strip().split(",")}
        if "rw" in options:
            return [self.fact(key, True, source_command=result.source_command)]
        if "ro" in options:
            return [self.fact(key, False, source_command=result.source_command)]
        return [
            self.fact(
                key,
                status=FactStatus.ERROR,
                source_command=result.source_command,
                raw_excerpt="neither rw nor ro in mount options",
            )
        ]

    # -- drives ---------------------------------------------------------------

    def _drives(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("disk.lsblk")
        if not result.ok:
            return [self.failed_from_result("disk.drive_count", result)]
        src = result.source_command
        facts: list[Fact] = []
        physical: list[tuple[str, str]] = []
        names = 0
        hdds = 0
        for row in parse_lsblk(result.stdout):
            name = row.get("NAME", "")
            if not _NAME.fullmatch(name):
                facts.append(
                    self.fact(
                        "disk.row",
                        status=FactStatus.ERROR,
                        source_command=src,
                        raw_excerpt="drive name has unexpected characters",
                    )
                )
                continue
            names += 1
            rotational = row.get("ROTA") == "1"
            hdds += rotational
            facts.append(
                self.fact(f"disk.{name}.rotational", rotational, source_command=src)
            )
            size = _int(row.get("SIZE", ""))
            if size is not None:
                facts.append(
                    self.fact(
                        f"disk.{name}.size_bytes", size, unit="B", source_command=src
                    )
                )
            if row.get("TRAN"):
                facts.append(
                    self.fact(f"disk.{name}.transport", row["TRAN"], source_command=src)
                )
            if row.get("MODEL", "").strip():
                facts.append(
                    self.fact(
                        f"disk.{name}.model",
                        row["MODEL"].strip()[:100],
                        source_command=src,
                    )
                )
            if row.get("TRAN") in _PHYSICAL_TRANSPORTS and _DEVICE_NAME.fullmatch(name):
                physical.append((name, row["TRAN"]))
        facts.insert(0, self.fact("disk.drive_count", names, source_command=src))
        facts.insert(1, self.fact("disk.hdd_count", hdds, source_command=src))
        facts += self._smart(ctx, physical)
        return facts

    def _smart(self, ctx: HostContext, drives: list[tuple[str, str]]) -> list[Fact]:
        """SMART facts per drive, plus totals across drives.

        A total is only reported as a real number when every drive it applies to
        was fully read. A non-zero total always stands: it is positive evidence
        of failure, and an unreadable drive cannot cancel it. A zero is only
        proof if nothing was left unread, so otherwise the total is unknown.
        """
        facts: list[Fact] = []
        failed = warn = 0
        totals = {"media": 0, "realloc": 0, "pending": 0, "uncorrect": 0}
        # Drives whose value could not be established, per diagnostic.
        health_missing = nvme_missing = sata_missing = 0
        for name, transport in drives:
            nvme_drive = transport == "nvme"
            result = ctx.runner.run("disk.smart_x", device=f"/dev/{name}")
            key = f"disk.{name}.smart_passed"
            usable = result.status in {
                CommandStatus.OK,
                CommandStatus.ERROR,
            } and _SMART_WORD.search(result.stdout)
            if not usable:
                facts.append(self.failed_from_result(key, result))
                health_missing += 1
                if nvme_drive:
                    nvme_missing += 1
                else:
                    sata_missing += 1
                continue
            src = result.source_command
            passed = smart_health(result.stdout)
            if passed is None:
                health_missing += 1
                facts.append(
                    self.fact(
                        key,
                        status=FactStatus.UNSUPPORTED,
                        source_command=src,
                        raw_excerpt="no SMART health line in smartctl output",
                    )
                )
            else:
                failed += not passed
                facts.append(self.fact(key, passed, source_command=src))
            nvme = parse_nvme(result.stdout)
            for field, value in nvme.items():
                facts.append(
                    self.fact(f"disk.{name}.{field}", value, source_command=src)
                )
            if isinstance(nvme.get("media_errors"), int):
                totals["media"] += nvme["media_errors"]  # type: ignore[operator]
            if nvme.get("critical_warning") not in (None, "", "0x00"):
                warn += 1
            if nvme_drive and not (
                isinstance(nvme.get("media_errors"), int)
                and nvme.get("critical_warning") not in (None, "")
            ):
                nvme_missing += 1  # an NVMe drive without its NVMe health fields
            if isinstance(nvme.get("available_spare_pct"), int) and isinstance(
                nvme.get("spare_threshold_pct"), int
            ):
                facts.append(
                    self.fact(
                        f"disk.{name}.spare_below_threshold",
                        nvme["available_spare_pct"] < nvme["spare_threshold_pct"],  # type: ignore[operator]
                        source_command=src,
                    )
                )
            ata = parse_ata_attributes(result.stdout)
            for field, value in ata.items():
                facts.append(
                    self.fact(f"disk.{name}.{field}", value, source_command=src)
                )
                if field == "reallocated_sectors":
                    totals["realloc"] += value
                elif field == "pending_sectors":
                    totals["pending"] += value
                elif field in {"reported_uncorrect", "offline_uncorrectable"}:
                    totals["uncorrect"] += value
            if not nvme_drive and not (
                "reallocated_sectors" in ata and "pending_sectors" in ata
            ):
                sata_missing += 1  # no attribute table (a SAS drive, or a failed read)

        if not drives:
            return facts

        def total(key: str, value: int, missing: int) -> Fact:
            if missing == 0 or value > 0:
                return self.fact(key, value)
            return self.fact(
                key,
                status=FactStatus.ERROR,
                raw_excerpt=(
                    f"{missing} drive(s) could not be fully read, "
                    "so a zero would not be proof"
                ),
            )

        facts += [
            total("disk.smart_failed_count", failed, health_missing),
            total("disk.nvme_critical_warning_count", warn, nvme_missing),
            total("disk.nvme_media_errors_total", totals["media"], nvme_missing),
            total("disk.sata_reallocated_total", totals["realloc"], sata_missing),
            total("disk.sata_pending_total", totals["pending"], sata_missing),
            total("disk.sata_uncorrectable_total", totals["uncorrect"], sata_missing),
        ]
        return facts
