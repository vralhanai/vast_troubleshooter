"""GPU, PCIe, and storage errors from the kernel log (current boot only).

Facts hold typed evidence only: counts, Xid codes, first and last time seen.
Raw log lines (which can carry hostnames, process names, and paths) are
analysed here and then dropped. They are never stored in a fact.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus

_XID = re.compile(r"NVRM: Xid \(PCI:([^)]+)\):\s*(\d+)", re.IGNORECASE)
_AER_HEADER = re.compile(
    r"AER: (?:Multiple )?(Corrected|Uncorrected \((?:Non-Fatal|Fatal)\)) error received",
    re.IGNORECASE,
)
_AER_SEVERITY = re.compile(
    r"PCIe Bus Error: severity=(Corrected|Uncorrected \((?:Non-Fatal|Fatal)\))",
    re.IGNORECASE,
)
_IO_ERROR = re.compile(r"I/O error|EXT4-fs error", re.IGNORECASE)
_ISO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_DMESG_TIME = re.compile(r"^\[\s*(\w{3} \w{3}\s+\d+ \d{2}:\d{2}:\d{2} \d{4})\]")

COUNT_KEYS = (
    "kernel_log.xid_count",
    "kernel_log.aer_corrected_count",
    "kernel_log.aer_uncorrected_count",
    "kernel_log.aer_fatal_count",
    "kernel_log.io_error_count",
)


def line_time(line: str) -> str | None:
    """Normalised 'YYYY-MM-DDTHH:MM:SS' from a journalctl or dmesg -T line."""
    iso = _ISO_TIME.match(line)
    if iso:
        return iso.group(1)
    dm = _DMESG_TIME.match(line)
    if dm:
        try:
            parsed = datetime.strptime(
                " ".join(dm.group(1).split()), "%a %b %d %H:%M:%S %Y"
            )
        except ValueError:
            return None
        return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    return None


def _aer_kind(severity: str) -> str:
    lowered = severity.lower()
    if "fatal" in lowered and "non-fatal" not in lowered:
        return "fatal"
    if lowered.startswith("uncorrected"):
        return "uncorrected"
    return "corrected"


@dataclass
class Analysis:
    xid_codes: Counter[int] = field(default_factory=Counter)
    xid_devices: set[str] = field(default_factory=set)
    xid_first: str | None = None
    xid_last: str | None = None
    aer_header: Counter[str] = field(default_factory=Counter)
    aer_severity: Counter[str] = field(default_factory=Counter)
    aer_last: str | None = None
    io_errors: int = 0
    io_last: str | None = None

    @property
    def xid_count(self) -> int:
        return sum(self.xid_codes.values())

    @property
    def aer(self) -> Counter[str]:
        """Events per kind. Headers are one per event; the severity line is the
        fallback for kernels that print only that form."""
        return self.aer_header if sum(self.aer_header.values()) else self.aer_severity


def analyze(text: str) -> Analysis:
    result = Analysis()
    for line in text.splitlines():
        stamp = line_time(line)
        xid = _XID.search(line)
        if xid:
            result.xid_codes[int(xid.group(2))] += 1
            result.xid_devices.add(xid.group(1).strip().lower())
            if stamp:
                result.xid_first = result.xid_first or stamp
                result.xid_last = stamp
        header = _AER_HEADER.search(line)
        severity = _AER_SEVERITY.search(line)
        if header:
            result.aer_header[_aer_kind(header.group(1))] += 1
        if severity:
            result.aer_severity[_aer_kind(severity.group(1))] += 1
        if (header or severity) and stamp:
            result.aer_last = stamp
        if _IO_ERROR.search(line):
            result.io_errors += 1
            if stamp:
                result.io_last = stamp
    return result


class KernelLogCollector(Collector):
    name: ClassVar[str] = "kernel_log"

    def collect(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("kernel.journal")
        source = "journalctl_current_boot"
        if _is_empty_journal(result):
            text = ""
        elif result.ok:
            text = result.stdout
        elif result.status in {CommandStatus.NEEDS_ROOT, CommandStatus.REFUSED}:
            return self._unreadable(result)
        else:  # journalctl missing, failed, or hung: try dmesg
            result = ctx.runner.run("kernel.dmesg")
            source = "dmesg"
            if not result.ok:
                return self._unreadable(result)
            text = result.stdout

        found = analyze(text)
        facts = self._facts(found, source, result)
        return (
            self._unprovable_when_truncated(facts, result)
            if result.truncated
            else facts
        )

    def _unprovable_when_truncated(
        self, facts: list[Fact], result: CommandResult
    ) -> list[Fact]:
        """Cut-off output only proves what it contains.

        A count above zero stands (the events are real). A zero, and the
        absence of Xid 79 from the code list, would claim something about the
        part of the log we never saw, so those become unknown instead.
        """
        note = "the kernel log output was cut off, so absence of events is not proven"
        out: list[Fact] = []
        for fact in facts:
            is_zero_count = fact.key in COUNT_KEYS and fact.value == 0
            no_79 = fact.key == "kernel_log.xid_codes" and "79" not in str(
                fact.value
            ).split(",")
            if is_zero_count or no_79:
                out.append(
                    self.fact(
                        fact.key,
                        status=FactStatus.ERROR,
                        source_command=result.source_command or None,
                        raw_excerpt=note,
                    )
                )
            else:
                out.append(fact)
        return out

    def _unreadable(self, result: CommandResult) -> list[Fact]:
        facts = [self.failed_from_result(key, result) for key in COUNT_KEYS]
        facts.append(self.failed_from_result("kernel_log.source", result))
        return facts

    def _facts(self, found: Analysis, source: str, result: CommandResult) -> list[Fact]:
        cmd = result.source_command or None
        facts = [
            self.fact("kernel_log.source", source, source_command=cmd),
            self.fact("kernel_log.truncated", result.truncated, source_command=cmd),
            self.fact("kernel_log.xid_count", found.xid_count, source_command=cmd),
        ]
        if found.xid_count:
            codes = sorted(found.xid_codes)
            facts.append(
                self.fact(
                    "kernel_log.xid_codes",
                    ",".join(str(c) for c in codes),
                    source_command=cmd,
                )
            )
            facts.append(
                self.fact(
                    "kernel_log.xid_device_count",
                    len(found.xid_devices),
                    source_command=cmd,
                )
            )
            for code in codes:
                facts.append(
                    self.fact(
                        f"kernel_log.xid.{code}.count",
                        found.xid_codes[code],
                        source_command=cmd,
                    )
                )
            facts += self._seen("kernel_log.xid", found.xid_first, found.xid_last, cmd)

        aer = found.aer
        facts += [
            self.fact(
                "kernel_log.aer_corrected_count", aer["corrected"], source_command=cmd
            ),
            self.fact(
                "kernel_log.aer_uncorrected_count",
                aer["uncorrected"],
                source_command=cmd,
            ),
            self.fact("kernel_log.aer_fatal_count", aer["fatal"], source_command=cmd),
        ]
        if found.aer_last:
            facts.append(
                self.fact(
                    "kernel_log.aer_last_seen", found.aer_last, source_command=cmd
                )
            )

        facts.append(
            self.fact("kernel_log.io_error_count", found.io_errors, source_command=cmd)
        )
        if found.io_last:
            facts.append(
                self.fact(
                    "kernel_log.io_error_last_seen", found.io_last, source_command=cmd
                )
            )
        return facts

    def _seen(
        self, prefix: str, first: str | None, last: str | None, cmd: str | None
    ) -> list[Fact]:
        facts = []
        if first:
            facts.append(self.fact(f"{prefix}_first_seen", first, source_command=cmd))
        if last:
            facts.append(self.fact(f"{prefix}_last_seen", last, source_command=cmd))
        return facts


def _is_empty_journal(result: CommandResult) -> bool:
    """`journalctl -g` may exit non-zero and print '-- No entries --' on no match."""
    return (
        result.status is CommandStatus.ERROR and "no entries" in result.stdout.lower()
    )
