"""What a fix is, and what running one can produce.

A fix is reviewed code, never rule data. It names the mutation operations it
runs (from the mutation allowlist, which is separate from the read allowlist),
what it changes, and how to tell it worked.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from vast_troubleshooter.runner import CommandResult

_FIX_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")


class PlanningRunner(Protocol):
    """A runner that can say exactly what it would run, without running it."""

    def plan(
        self, op_id: str, **operands: str
    ) -> tuple[list[str], CommandResult | None]: ...

    def run(self, op_id: str, **operands: str) -> CommandResult: ...


class FixStatus(StrEnum):
    DRY_RUN = "dry_run"  # shown, nothing changed
    BLOCKED = "blocked"  # a check stopped it, nothing changed
    SUCCEEDED = "succeeded"  # ran, and the problem check now passes
    FAILED = (
        "failed"  # ran (or tried), and the problem is still there or a command failed
    )
    INDETERMINATE = "indeterminate"  # ran, and the result cannot be told


@dataclass(frozen=True)
class Fix:
    id: str
    description: str
    # What it touches, what should change, and how to undo it. Shown at dry run.
    target: str
    expected_change: str
    undo_notes: str
    # Mutation operation ids, run in order. Fixed: no operands.
    commands: tuple[str, ...]
    requires_root: bool = True
    requires_no_active_rental: bool = True
    # The rule whose check must FAIL before and PASS after. None: no check.
    resolves_rule: str | None = None
    # Collectors re-run before and after to observe the change.
    affected_collectors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _FIX_ID.fullmatch(self.id):
            raise ValueError(f"bad fix id {self.id!r}")
        if not self.commands:
            raise ValueError(f"{self.id}: a fix needs at least one command")
        for text in (
            self.description,
            self.target,
            self.expected_change,
            self.undo_notes,
        ):
            if not text.strip():
                raise ValueError(
                    f"{self.id}: description, target, expected change and undo notes are required"
                )


@dataclass(frozen=True)
class FixOutcome:
    status: FixStatus
    fix_id: str
    run_id: str
    reasons: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    target: str = ""
    expected_change: str = ""
    undo_notes: str = ""
    rental_state: str = ""
    # Check status of the fix's rule before and after, when it has one.
    before: str = ""
    after: str = ""
    block_code: str = ""

    @property
    def changed_system(self) -> bool:
        return self.status in {
            FixStatus.SUCCEEDED,
            FixStatus.FAILED,
            FixStatus.INDETERMINATE,
        }


@dataclass
class Registry:
    fixes: dict[str, Fix] = field(default_factory=dict)
