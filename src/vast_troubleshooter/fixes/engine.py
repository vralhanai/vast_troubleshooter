"""The only place that changes the system.

Order of checks, each of which stops the run and says why, with nothing changed:

  1. the fix is on the whitelist
  2. root is available if the fix needs it
  3. no rental is active (rental state is `none`), if the fix needs that
  4. the problem the fix resolves is actually present
  -- dry run stops here (the default) --
  5. the operator typed the fix id exactly
  6. this tool's own lock is free
  7. the action log is writable (no unlogged change, ever)
  8. rental state and the problem are checked AGAIN, immediately before the change

Then each command runs in order, the affected collectors run again, and the
rule the fix resolves must now pass. A timeout is judged by what is observed
afterwards, or reported as indeterminate. Rollback does not exist for a daemon
restart: an interrupted workload cannot be restored.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from vast_troubleshooter.collectors import get_collectors, run_collectors
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.fixes.base import Fix, FixOutcome, FixStatus, PlanningRunner
from vast_troubleshooter.fixes.storage import (
    ActionLog,
    LockBusyError,
    ProcessLock,
    state_dir,
)
from vast_troubleshooter.models import CheckStatus, Fact
from vast_troubleshooter.rentals import RentalAssessment, RentalState, check_rentals
from vast_troubleshooter.rule_engine import Rule, evaluate_rules
from vast_troubleshooter.runner import CommandStatus

RentalCheck = Callable[[HostContext], RentalAssessment]
Observe = Callable[[Fix], list[Fact]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FixEngine:
    def __init__(
        self,
        fixes: Mapping[str, Fix],
        mutation_runner: PlanningRunner,
        ctx: HostContext,
        rules: Sequence[Rule],
        *,
        log: ActionLog | None = None,
        lock_path: Path | None = None,
        rental_check: RentalCheck = check_rentals,
        observe: Observe | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._fixes = dict(fixes)
        self._runner = mutation_runner
        self._ctx = ctx
        self._rules = {rule.id: rule for rule in rules}
        for fix in self._fixes.values():
            if fix.resolves_rule and fix.resolves_rule not in self._rules:
                raise ValueError(f"{fix.id}: unknown rule {fix.resolves_rule!r}")
        directory = state_dir()
        self._log = log or ActionLog(directory / "actions.jsonl")
        self._lock_path = lock_path or directory / "fix.lock"
        self._rental_check = rental_check
        self._observe = observe or self._observe_real
        self._clock = clock

    # -- observation ----------------------------------------------------------

    def _observe_real(self, fix: Fix) -> list[Fact]:
        return run_collectors(get_collectors(fix.affected_collectors), self._ctx)

    def _rule_status(self, fix: Fix) -> str:
        if not fix.resolves_rule:
            return ""
        facts = self._observe(fix)
        (result,) = evaluate_rules([self._rules[fix.resolves_rule]], facts)
        return result.status.value

    def _rental(self) -> tuple[RentalState, str]:
        assessment = self._rental_check(self._ctx)
        return assessment.state_at(self._clock()), assessment.reason

    # -- the run ----------------------------------------------------------------

    def run(
        self, fix_id: str, *, apply: bool = False, confirmation: str | None = None
    ) -> FixOutcome:
        run_id = uuid.uuid4().hex[:12]
        fix = self._fixes.get(fix_id)
        if fix is None:
            return self._blocked(run_id, None, fix_id, "unknown_fix", "not a known fix")

        plans: list[list[str]] = []
        for op_id in fix.commands:
            argv, early = self._runner.plan(op_id)
            if early is not None:
                code = (
                    "needs_root"
                    if early.status is CommandStatus.NEEDS_ROOT
                    else "not_allowed"
                )
                return self._blocked(
                    run_id, fix, fix_id, code, early.detail or early.status.value
                )
            plans.append(argv)

        rental = ""
        if fix.requires_no_active_rental:
            state, why = self._rental()
            rental = state.value
            if state is not RentalState.NONE:
                code = (
                    "rental_active" if state is RentalState.ACTIVE else "rental_unknown"
                )
                return self._blocked(
                    run_id,
                    fix,
                    fix_id,
                    code,
                    f"rental state is {state.value}: {why}",
                    rental,
                )

        before = self._rule_status(fix)
        if fix.resolves_rule and before != CheckStatus.FAIL.value:
            reason = {
                "pass": "nothing to fix: the check already passes",
                "unknown": "cannot confirm the problem exists: the check is unknown",
                "skipped": "the check does not apply to this host",
            }.get(before, f"the check is {before}")
            return self._blocked(
                run_id, fix, fix_id, "no_problem", reason, rental, before
            )

        commands = tuple(tuple(argv) for argv in plans)
        if not apply:
            outcome = self._outcome(
                FixStatus.DRY_RUN, run_id, fix, commands, rental=rental, before=before
            )
            self._safe_log(outcome, "dry_run")
            return outcome

        if confirmation != fix.id:
            return self._blocked(
                run_id,
                fix,
                fix_id,
                "confirmation_mismatch",
                "the fix id was not typed exactly; nothing was changed",
                rental,
                before,
            )

        lock = ProcessLock(self._lock_path)
        try:
            lock.acquire()
        except LockBusyError:
            return self._blocked(
                run_id,
                fix,
                fix_id,
                "locked",
                "another fix run is in progress",
                rental,
                before,
            )
        except OSError as exc:
            return self._blocked(
                run_id,
                fix,
                fix_id,
                "lock_unavailable",
                f"cannot take the lock: {type(exc).__name__}",
                rental,
                before,
            )
        try:
            return self._apply(run_id, fix, commands, before)
        finally:
            lock.release()

    def _apply(
        self, run_id: str, fix: Fix, commands: tuple[tuple[str, ...], ...], before: str
    ) -> FixOutcome:
        # Nothing may change unless it can be logged first.
        try:
            self._log.write(
                {
                    "run_id": run_id,
                    "fix_id": fix.id,
                    "phase": "apply_started",
                    "commands": [list(c) for c in commands],
                    "before": before,
                }
            )
        except OSError as exc:
            return self._blocked(
                run_id,
                fix,
                fix.id,
                "log_unwritable",
                f"cannot write the action log: {type(exc).__name__}",
                "",
                before,
            )

        # Re-check immediately before the change: confirmation takes time, and
        # the host can change while the operator is typing.
        state, why = ("", "")
        if fix.requires_no_active_rental:
            now_state, why = self._rental()
            state = now_state.value
            if now_state is not RentalState.NONE:
                return self._blocked(
                    run_id,
                    fix,
                    fix.id,
                    "state_changed",
                    f"rental state is now {state}: {why}",
                    state,
                    before,
                )
        again = self._rule_status(fix)
        if fix.resolves_rule and again != CheckStatus.FAIL.value:
            return self._blocked(
                run_id,
                fix,
                fix.id,
                "state_changed",
                f"the check is now {again}; nothing was changed",
                state,
                before,
            )

        timed_out = False
        failed = ""
        for op_id in fix.commands:
            result = self._runner.run(op_id)
            self._log.write(
                {
                    "run_id": run_id,
                    "fix_id": fix.id,
                    "phase": "command",
                    "op_id": op_id,
                    "status": result.status.value,
                    "returncode": result.returncode,
                }
            )
            if result.status is CommandStatus.TIMEOUT:
                timed_out = True
                failed = f"{op_id} timed out"
                break
            if not result.ok:
                failed = f"{op_id} failed ({result.status.value})"
                break

        after = self._rule_status(fix)
        status, reasons = self._judge(fix, failed, timed_out, after)
        outcome = self._outcome(
            status,
            run_id,
            fix,
            commands,
            reasons=reasons,
            rental=state,
            before=before,
            after=after,
        )
        self._log.write(
            {
                "run_id": run_id,
                "fix_id": fix.id,
                "phase": "apply_finished",
                "status": status.value,
                "reasons": list(reasons),
                "before": before,
                "after": after,
                "undo_notes": fix.undo_notes,
            }
        )
        return outcome

    @staticmethod
    def _judge(
        fix: Fix, failed: str, timed_out: bool, after: str
    ) -> tuple[FixStatus, tuple[str, ...]]:
        """Decide from what was observed afterwards, never from hope."""
        if fix.resolves_rule is None:
            if failed:
                return FixStatus.FAILED, (failed,)
            return FixStatus.SUCCEEDED, (
                "commands ran; this fix has no check to confirm the result",
            )
        if after == CheckStatus.PASS.value:
            note = (
                (f"{failed}, but the check now passes",)
                if failed
                else ("the check now passes",)
            )
            if failed and not timed_out:
                return FixStatus.FAILED, (
                    f"{failed}; the check passes, but the command did not report success",
                )
            return FixStatus.SUCCEEDED, note
        if after == CheckStatus.FAIL.value:
            return FixStatus.FAILED, (
                (failed + "; " if failed else "") + "the problem is still present",
            )
        reason = f"the check is {after or 'unknown'} afterwards, so the result cannot be told"
        return FixStatus.INDETERMINATE, ((failed + "; " if failed else "") + reason,)

    # -- helpers ----------------------------------------------------------------

    def _outcome(
        self,
        status: FixStatus,
        run_id: str,
        fix: Fix,
        commands: tuple[tuple[str, ...], ...],
        *,
        reasons: tuple[str, ...] = (),
        rental: str = "",
        before: str = "",
        after: str = "",
        block_code: str = "",
    ) -> FixOutcome:
        return FixOutcome(
            status=status,
            fix_id=fix.id,
            run_id=run_id,
            reasons=reasons,
            commands=commands,
            target=fix.target,
            expected_change=fix.expected_change,
            undo_notes=fix.undo_notes,
            rental_state=rental,
            before=before,
            after=after,
            block_code=block_code,
        )

    def _blocked(
        self,
        run_id: str,
        fix: Fix | None,
        fix_id: str,
        code: str,
        reason: str,
        rental: str = "",
        before: str = "",
    ) -> FixOutcome:
        outcome = FixOutcome(
            status=FixStatus.BLOCKED,
            fix_id=fix_id,
            run_id=run_id,
            reasons=(reason,),
            target=fix.target if fix else "",
            expected_change=fix.expected_change if fix else "",
            undo_notes=fix.undo_notes if fix else "",
            rental_state=rental,
            before=before,
            block_code=code,
        )
        self._safe_log(outcome, "blocked")
        return outcome

    def _safe_log(self, outcome: FixOutcome, phase: str) -> None:
        """Blocked and dry runs change nothing, so a log failure must not hide them."""
        try:
            self._log.write(
                {
                    "run_id": outcome.run_id,
                    "fix_id": outcome.fix_id,
                    "phase": phase,
                    "status": outcome.status.value,
                    "block_code": outcome.block_code,
                    "reasons": list(outcome.reasons),
                    "commands": [list(c) for c in outcome.commands],
                    "rental_state": outcome.rental_state,
                    "undo_notes": outcome.undo_notes,
                }
            )
        except OSError:
            pass
