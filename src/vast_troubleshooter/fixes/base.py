"""What a fix is, and what running one can produce.

A fix is reviewed code, never rule data. It names the mutation operations it
runs (from the mutation allowlist, which is separate from the read allowlist),
what it changes, how to tell it worked, and how to back out.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact
from vast_troubleshooter.runner import CommandResult

_FIX_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")

# Returns None if it is safe to go on, or the reason it is not. Reviewed code
# reading facts: it can only refuse, never approve something the engine refused.
Precheck = Callable[[Mapping[str, Fact]], str | None]


class OperandsUnavailable(Exception):
    """The target cannot be decided, or acting on it would not be safe. Says why."""


# Works out the one target a fix acts on (for example the network link) from the
# host itself. None means it could not be decided, so the fix does not run.
OperandSource = Callable[[HostContext], Mapping[str, str] | None]


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
    FAILED = "failed"  # a command failed or the problem is still there
    INDETERMINATE = "indeterminate"  # ran, and the result cannot be told


@dataclass(frozen=True)
class FixStep:
    """One mutation operation. `operands` names which of the fix's operands
    (from its operand source) this operation takes."""

    op_id: str
    operands: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fix:
    id: str
    description: str
    # What it touches, what should change, and how to undo it. Shown at dry run.
    target: str
    expected_change: str
    undo_notes: str
    commands: tuple[FixStep | str, ...]
    requires_root: bool = True
    requires_no_active_rental: bool = True
    # The rule whose check must FAIL before and PASS after. None: no check.
    resolves_rule: str | None = None
    # Collectors re-run before and after to observe the change.
    affected_collectors: tuple[str, ...] = ()
    # Extra conditions that must hold, before and again right before the change.
    precheck: Precheck | None = None
    operand_source: OperandSource | None = None
    # Run automatically if the result is not a success. Empty: nothing to roll back.
    rollback: tuple[FixStep | str, ...] = ()
    # Wait this long after the commands before observing, so a service that
    # starts and then crashes is not reported as fixed.
    settle_seconds: float = 0.0
    # Only for a disposable test machine. Refused unless the operator says so.
    demo_only: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def command_steps(self) -> tuple[FixStep, ...]:
        """`commands` as steps (a bare op id is turned into a step in __post_init__)."""
        return cast("tuple[FixStep, ...]", self.commands)

    @property
    def rollback_steps(self) -> tuple[FixStep, ...]:
        return cast("tuple[FixStep, ...]", self.rollback)

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
        if self.settle_seconds < 0 or self.settle_seconds > 120:
            raise ValueError(f"{self.id}: settle_seconds must be between 0 and 120")
        # Plain strings are shorthand for a step with no operands.
        for name in ("commands", "rollback"):
            steps = tuple(
                s if isinstance(s, FixStep) else FixStep(s) for s in getattr(self, name)
            )
            object.__setattr__(self, name, steps)


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
    warnings: tuple[str, ...] = ()
    rental_state: str = ""
    # Check status of the fix's rule before and after, when it has one.
    before: str = ""
    after: str = ""
    block_code: str = ""
    rolled_back: bool = False
    # The undo itself failed, so the change may still be in place.
    rollback_failed: bool = False

    @property
    def changed_system(self) -> bool:
        return self.status in {
            FixStatus.SUCCEEDED,
            FixStatus.FAILED,
            FixStatus.INDETERMINATE,
        }
