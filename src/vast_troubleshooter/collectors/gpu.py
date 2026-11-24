"""GPU facts read through nvidia-smi and lspci."""

import csv
import re
from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus, Scalar
from vast_troubleshooter.operations import GPU_QUERY_FIELDS
from vast_troubleshooter.runner import CommandResult

_UNSUPPORTED = {"[n/a]", "n/a", "[not supported]", "not supported", "[not found]"}
_PCI_GPU_CLASS = re.compile(r"\[(0300|0302)\]")

# clocks_event_reasons bitmask, from the NVIDIA NVML documentation.
THROTTLE_BITS = {
    0x1: "gpu_idle",
    0x2: "applications_clocks_setting",
    0x4: "sw_power_cap",
    0x8: "hw_slowdown",
    0x10: "sync_boost",
    0x20: "sw_thermal_slowdown",
    0x40: "hw_thermal_slowdown",
    0x80: "hw_power_brake_slowdown",
    0x100: "display_clock_setting",
}


def parse_field(raw: str, kind: str) -> tuple[Scalar, FactStatus, str | None]:
    """Parse one nvidia-smi csv value into (value, status, note)."""
    text = raw.strip()
    lowered = text.lower()
    if lowered in _UNSUPPORTED:
        return None, FactStatus.UNSUPPORTED, text
    if (
        lowered.startswith("err")
        or "unknown error" in lowered
        or "gpu is lost" in lowered
        or "requires reset" in lowered
    ):
        return None, FactStatus.ERROR, text
    try:
        if kind == "int":
            return int(text), FactStatus.OK, None
        if kind == "float":
            return float(text), FactStatus.OK, None
    except ValueError:
        return None, FactStatus.ERROR, f"unparseable: {text[:50]}"
    return text, FactStatus.OK, None


def decode_throttle(hex_text: str) -> tuple[str, str | None]:
    """Return (comma-separated reason names or 'none', error note)."""
    try:
        mask = int(hex_text.strip(), 16)
    except ValueError:
        return "", f"unparseable: {hex_text.strip()[:50]}"
    names = [name for bit, name in THROTTLE_BITS.items() if mask & bit]
    return (",".join(names) if names else "none"), None


_MAX_CSV_LINE = 4096


def _rows(text: str) -> list[list[str]]:
    """CSV rows. A line that cannot be a real nvidia-smi row (too long, or
    unparseable) becomes a one-field row, which the callers already treat as
    malformed, instead of raising."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line) > _MAX_CSV_LINE:
            rows.append(["<line too long>"])
            continue
        try:
            rows.extend(csv.reader([line], skipinitialspace=True))
        except csv.Error:
            rows.append(["<unparseable line>"])
    return rows


class GpuCollector(Collector):
    name: ClassVar[str] = "gpu"

    def collect(self, ctx: HostContext) -> list[Fact]:
        facts: list[Fact] = []
        query = ctx.runner.run("gpu.query")
        indexes: list[int] = []
        if query.ok:
            facts += self._per_gpu(query, indexes)
        elif "no devices were found" in (query.stdout + query.stderr).lower():
            facts.append(
                self.fact(
                    "gpu.count",
                    0,
                    source_command=query.source_command,
                    raw_excerpt="nvidia-smi reports no devices",
                )
            )
        else:
            facts.append(self.failed_from_result("gpu.count", query))
        facts.append(self._pci_count(ctx))
        facts += self._throttle(ctx)
        return facts

    def _per_gpu(self, query: CommandResult, indexes: list[int]) -> list[Fact]:
        source = query.source_command
        facts: list[Fact] = []
        names: list[str] = []
        vram: list[int] = []
        rows = _rows(query.stdout)
        expected = 1 + len(GPU_QUERY_FIELDS)
        malformed = 0
        for position, row in enumerate(rows):
            if len(row) != expected:
                malformed += 1
                facts.append(
                    self.fact(
                        f"gpu.{position}.row",
                        status=FactStatus.ERROR,
                        source_command=source,
                        raw_excerpt=f"expected {expected} fields, got {len(row)}",
                    )
                )
                continue
            try:
                index = int(row[0])
            except ValueError:
                index = position
            indexes.append(index)
            for (_query, suffix, kind, unit), raw in zip(
                GPU_QUERY_FIELDS, row[1:], strict=True
            ):
                value, status, note = parse_field(raw, kind)
                facts.append(
                    self.fact(
                        f"gpu.{index}.{suffix}",
                        value,
                        unit=unit,
                        status=status,
                        source_command=source,
                        raw_excerpt=note,
                    )
                )
                if status is FactStatus.OK and suffix == "name":
                    names.append(str(value))
                if status is FactStatus.OK and suffix == "memory_total":
                    vram.append(int(value))  # type: ignore[arg-type]
        # A count made from only the rows that could be read would understate the
        # GPUs (or say zero), so with any unreadable row the count is not claimed.
        count = (
            self.fact(
                "gpu.count",
                status=FactStatus.ERROR,
                source_command=source,
                raw_excerpt=f"{malformed} nvidia-smi row(s) could not be parsed",
            )
            if malformed
            else self.fact("gpu.count", len(indexes), source_command=source)
        )
        facts.insert(0, count)
        if names:
            facts.append(
                self.fact(
                    "gpu.models_identical", len(set(names)) == 1, source_command=source
                )
            )
        if vram:
            facts.append(
                self.fact(
                    "gpu.vram_min_mib", min(vram), unit="MiB", source_command=source
                )
            )
        return facts

    def _pci_count(self, ctx: HostContext) -> Fact:
        result = ctx.runner.run("gpu.lspci_nvidia")
        if not result.ok:
            return self.failed_from_result("gpu.pci_count", result)
        count = sum(1 for ln in result.stdout.splitlines() if _PCI_GPU_CLASS.search(ln))
        return self.fact("gpu.pci_count", count, source_command=result.source_command)

    def _throttle(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("gpu.throttle_event")
        if not result.ok:
            result = ctx.runner.run("gpu.throttle_legacy")
        if not result.ok:
            return [self.failed_from_result("gpu.throttle_reasons", result)]
        facts = []
        for position, row in enumerate(_rows(result.stdout)):
            if len(row) != 2:
                facts.append(
                    self.fact(
                        f"gpu.{position}.throttle_reasons",
                        status=FactStatus.ERROR,
                        source_command=result.source_command,
                        raw_excerpt=f"expected 2 fields, got {len(row)}",
                    )
                )
                continue
            try:
                index = int(row[0])
            except ValueError:
                index = position
            key = f"gpu.{index}.throttle_reasons"
            if row[1].strip().lower() in _UNSUPPORTED:
                facts.append(
                    self.fact(
                        key,
                        status=FactStatus.UNSUPPORTED,
                        source_command=result.source_command,
                    )
                )
                continue
            names, problem = decode_throttle(row[1])
            if problem:
                facts.append(
                    self.fact(
                        key,
                        status=FactStatus.ERROR,
                        source_command=result.source_command,
                        raw_excerpt=problem,
                    )
                )
            else:
                facts.append(
                    self.fact(
                        key,
                        names,
                        source_command=result.source_command,
                        raw_excerpt=row[1].strip(),
                    )
                )
        return facts
