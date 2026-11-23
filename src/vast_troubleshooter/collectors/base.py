"""Collector interface and the loop that runs collectors safely."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import ClassVar

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus, Scalar
from vast_troubleshooter.runner import CommandResult, CommandStatus

_STATUS_MAP = {
    CommandStatus.OK: FactStatus.OK,
    CommandStatus.MISSING: FactStatus.MISSING,
    CommandStatus.TIMEOUT: FactStatus.TIMEOUT,
    CommandStatus.ERROR: FactStatus.ERROR,
    CommandStatus.REFUSED: FactStatus.ERROR,
    CommandStatus.NEEDS_ROOT: FactStatus.NEEDS_ROOT,
}


def fact_status(result: CommandResult) -> FactStatus:
    return _STATUS_MAP[result.status]


class Collector(ABC):
    """Reads one area of the host and returns facts. Read-only."""

    name: ClassVar[str]

    @abstractmethod
    def collect(self, ctx: HostContext) -> list[Fact]:
        """Return facts. Failures become facts with a non-ok status."""

    def fact(
        self,
        key: str,
        value: Scalar = None,
        *,
        unit: str | None = None,
        status: FactStatus = FactStatus.OK,
        source_command: str | None = None,
        raw_excerpt: str | None = None,
    ) -> Fact:
        return Fact(
            collector=self.name,
            key=key,
            value=value,
            unit=unit,
            status=status,
            source_command=source_command,
            raw_excerpt=raw_excerpt[:2000] if raw_excerpt else None,
        )

    def failed_from_result(self, key: str, result: CommandResult) -> Fact:
        """A fact for a command that did not succeed."""
        detail = result.detail
        text = (result.stderr or result.stdout).strip()[:300]
        if text:
            detail = f"{detail}: {text}".strip(": ")
        return self.fact(
            key,
            status=fact_status(result),
            source_command=result.source_command or None,
            raw_excerpt=detail or None,
        )


def run_collectors(collectors: Iterable[Collector], ctx: HostContext) -> list[Fact]:
    """Run every collector. One that raises becomes an error fact, not a crash."""
    facts: list[Fact] = []
    for collector in collectors:
        try:
            facts.extend(collector.collect(ctx))
        except Exception as exc:  # noqa: BLE001 - a collector must never crash the run
            facts.append(
                Fact(
                    collector=collector.name,
                    key=f"{collector.name}.collector",
                    status=FactStatus.ERROR,
                    raw_excerpt=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
    return facts
