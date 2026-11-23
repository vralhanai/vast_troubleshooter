"""Collector registry."""

from collections.abc import Iterable

from vast_troubleshooter.collectors.base import Collector, run_collectors
from vast_troubleshooter.collectors.docker import DockerCollector
from vast_troubleshooter.collectors.driver import DriverCollector
from vast_troubleshooter.collectors.gpu import GpuCollector
from vast_troubleshooter.collectors.kernel_log import KernelLogCollector
from vast_troubleshooter.collectors.system import SystemCollector

__all__ = [
    "COLLECTORS",
    "Collector",
    "UnknownCollectorError",
    "get_collectors",
    "run_collectors",
]

COLLECTORS: dict[str, Collector] = {
    c.name: c
    for c in (
        SystemCollector(),
        GpuCollector(),
        DriverCollector(),
        DockerCollector(),
        KernelLogCollector(),
    )
}


class UnknownCollectorError(LookupError):
    pass


def get_collectors(names: Iterable[str] | None = None) -> list[Collector]:
    """All collectors, or the named ones in the given order."""
    if names is None:
        return list(COLLECTORS.values())
    selected = []
    for name in names:
        if name not in COLLECTORS:
            known = ", ".join(sorted(COLLECTORS))
            raise UnknownCollectorError(f"unknown collector {name!r} (known: {known})")
        selected.append(COLLECTORS[name])
    return selected
