"""NVIDIA driver and CUDA version, read from the nvidia-smi header."""

import re
from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandStatus

_DRIVER = re.compile(r"Driver Version:\s*(\S+)")
_CUDA = re.compile(r"CUDA Version:\s*(\S+)")
_CUDA_NUMBER = re.compile(r"(\d+)\.(\d+)")
_NO_COMM = "couldn't communicate with the nvidia driver"


def cuda_code(version: str) -> int | None:
    """CUDA's own integer encoding: 11.8 -> 11080, 12.6 -> 12060."""
    match = _CUDA_NUMBER.fullmatch(version)
    if not match:
        return None
    return int(match.group(1)) * 1000 + int(match.group(2)) * 10


class DriverCollector(Collector):
    name: ClassVar[str] = "driver"

    def collect(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("driver.smi_summary")
        source = result.source_command or None
        text = f"{result.stdout}\n{result.stderr}"

        if result.status is CommandStatus.MISSING:
            return [
                self.fact(key, status=FactStatus.MISSING, source_command=source)
                for key in (
                    "driver.nvidia_smi_responding",
                    "driver.version",
                    "driver.cuda_version",
                )
            ]

        if not result.ok:
            # nvidia-smi ran but failed or hung. That is an observation, not
            # an unknown: it is a signal of a broken driver.
            reason = (
                "cannot communicate with the NVIDIA driver"
                if _NO_COMM in text.lower()
                else result.detail
            )
            failed = self.failed_from_result
            return [
                self.fact(
                    "driver.nvidia_smi_responding",
                    False,
                    source_command=source,
                    raw_excerpt=reason,
                ),
                failed("driver.version", result),
                failed("driver.cuda_version", result),
            ]

        facts = [self.fact("driver.nvidia_smi_responding", True, source_command=source)]
        driver = _DRIVER.search(text)
        facts.append(
            self.fact("driver.version", driver.group(1), source_command=source)
            if driver
            else self.fact(
                "driver.version",
                status=FactStatus.ERROR,
                source_command=source,
                raw_excerpt="no 'Driver Version' in nvidia-smi output",
            )
        )
        facts += self._cuda(text, source)
        return facts

    def _cuda(self, text: str, source: str | None) -> list[Fact]:
        match = _CUDA.search(text)
        if not match:
            return [
                self.fact(
                    "driver.cuda_version",
                    status=FactStatus.ERROR,
                    source_command=source,
                    raw_excerpt="no 'CUDA Version' in nvidia-smi output",
                )
            ]
        version = match.group(1)
        code = cuda_code(version)
        if code is None:
            return [
                self.fact(
                    "driver.cuda_version",
                    status=FactStatus.UNSUPPORTED,
                    source_command=source,
                    raw_excerpt=version,
                )
            ]
        return [
            self.fact("driver.cuda_version", version, source_command=source),
            self.fact("driver.cuda_version_code", code, source_command=source),
        ]
