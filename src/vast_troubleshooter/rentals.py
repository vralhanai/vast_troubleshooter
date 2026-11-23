"""Is a customer rental active on this host? Fail closed.

Vast does not document how a rental appears on the host, and no real host has
been inspected. So this module decides nothing it cannot prove:

* `active`  - some source found evidence that a workload may exist.
* `none`    - EVERY required source looked, readably and recently, and found none.
* `unknown` - anything else: a source could not look, did not exist yet, was
              unreadable, timed out, or its evidence is stale.

Only `none` allows a disruptive action. `active` and `unknown` both block it,
even if the operator confirms. Positive evidence always wins over absence.

Sources are conservative on purpose. Any Docker container, running or stopped,
counts as a possible rental (stopped ones can still belong to a customer, and
without a real host we cannot tell a rental from the host's own containers).
That can over-block. It cannot under-block.

The VM source is a stub that always says `unknown`, so `none` cannot be
produced until it is implemented from real evidence. See docs/rental-detection.md.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from vast_troubleshooter.context import HostContext

MAX_EVIDENCE_AGE = timedelta(seconds=30)
MAX_CLOCK_SKEW = timedelta(seconds=5)
REQUIRED_SOURCES = ("containers", "vms")


class RentalState(StrEnum):
    ACTIVE = "active"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceResult:
    state: RentalState
    reason: str
    detail: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceReading:
    source: str
    state: RentalState
    reason: str
    at: datetime
    detail: dict[str, int] = field(default_factory=dict)


class EvidenceSource(Protocol):
    name: str

    def read(self, ctx: HostContext) -> SourceResult: ...


def _unknown(reason: str) -> SourceResult:
    return SourceResult(RentalState.UNKNOWN, reason)


class ContainersSource:
    """Every Docker container counts, in any state. Only the state is read:
    never names, ids, images, environment, or logs."""

    name = "containers"

    def read(self, ctx: HostContext) -> SourceResult:
        result = ctx.runner.run("rentals.docker_ps_all")
        if not result.ok:
            return _unknown(f"docker ps could not run ({result.status.value})")
        if result.truncated:
            return _unknown("docker ps output was truncated")
        states = [s.strip().lower() for s in result.stdout.splitlines() if s.strip()]
        total = len(states)
        running = sum(1 for s in states if s == "running")
        detail = {"total": total, "running": running}
        if total == 0:
            return SourceResult(RentalState.NONE, "no containers exist", detail)
        return SourceResult(
            RentalState.ACTIVE,
            f"{total} container(s) exist, {running} running; "
            "any container is treated as a possible rental",
            detail,
        )


class VmsSource:
    """Not implemented: the docs do not say how a VM rental appears on the host."""

    name = "vms"

    def read(self, ctx: HostContext) -> SourceResult:
        return _unknown(
            "VM rental detection is not implemented "
            "(the Vast docs do not say how VM rentals appear on the host)"
        )


def default_sources() -> tuple[EvidenceSource, ...]:
    return (ContainersSource(), VmsSource())


@dataclass(frozen=True)
class RentalAssessment:
    state: RentalState
    readings: tuple[SourceReading, ...]
    assessed_at: datetime
    reason: str = ""

    def state_at(
        self, now: datetime, max_age: timedelta = MAX_EVIDENCE_AGE
    ) -> RentalState:
        """The state, downgraded to unknown if `none` rests on old evidence.

        Positive evidence is never downgraded: an old `active` still blocks.
        """
        if self.state is not RentalState.NONE:
            return self.state
        for reading in self.readings:
            age = now - reading.at
            if age > max_age or age < -MAX_CLOCK_SKEW:
                return RentalState.UNKNOWN
        return RentalState.NONE

    @property
    def blocks_disruption(self) -> bool:
        return self.state is not RentalState.NONE

    @property
    def unresolved(self) -> list[str]:
        return [r.source for r in self.readings if r.state is RentalState.UNKNOWN]


def blocks_disruption(state: RentalState) -> bool:
    """Only `none` permits a disruptive action."""
    return state is not RentalState.NONE


def _utcnow() -> datetime:
    return datetime.now(UTC)


def assess(
    readings: Sequence[SourceReading],
    required: Sequence[str] = REQUIRED_SOURCES,
    now: datetime | None = None,
) -> RentalAssessment:
    stamp = now or _utcnow()
    active = [r for r in readings if r.state is RentalState.ACTIVE]
    if active:
        return RentalAssessment(
            RentalState.ACTIVE, tuple(readings), stamp, active[0].reason
        )
    seen = {r.source for r in readings}
    missing = [name for name in required if name not in seen]
    if missing:
        return RentalAssessment(
            RentalState.UNKNOWN,
            tuple(readings),
            stamp,
            f"required source(s) did not report: {', '.join(missing)}",
        )
    unknown = [r for r in readings if r.state is RentalState.UNKNOWN]
    if unknown:
        return RentalAssessment(
            RentalState.UNKNOWN, tuple(readings), stamp, unknown[0].reason
        )
    return RentalAssessment(
        RentalState.NONE,
        tuple(readings),
        stamp,
        "every required source found no rental",
    )


def check_rentals(
    ctx: HostContext,
    sources: Sequence[EvidenceSource] | None = None,
    required: Sequence[str] = REQUIRED_SOURCES,
    clock: Callable[[], datetime] = _utcnow,
) -> RentalAssessment:
    """Ask every source now. A source that fails is `unknown`, never an exception."""
    readings = []
    for source in default_sources() if sources is None else sources:
        try:
            result = source.read(ctx)
        except Exception as exc:  # noqa: BLE001 - a broken source must fail closed
            result = _unknown(f"source failed: {type(exc).__name__}")
        readings.append(
            SourceReading(
                source.name, result.state, result.reason, clock(), result.detail
            )
        )
    return assess(readings, required, clock())
