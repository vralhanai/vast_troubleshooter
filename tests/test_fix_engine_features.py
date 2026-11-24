"""Engine features added with the first fixes: preconditions, operands, rollback,
settle time, and the demo-only gate."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.fixes import ActionLog, Fix, FixEngine, FixStatus
from vast_troubleshooter.fixes.base import FixStep
from vast_troubleshooter.models import Fact
from vast_troubleshooter.rentals import RentalAssessment, RentalState, SourceReading
from vast_troubleshooter.rule_engine import load_rules_text
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeMutationRunner, fake_context

T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
RULES = load_rules_text(
    """
schema_version = 1
[[rule]]
id = "t.rule"
title = "t is broken"
severity = "critical"
category = "offline"
source = "test"
checked = "2026-01-01"
[rule.when]
type = "equals"
fact = "t.broken"
value = true
"""
)
BASE = Fix(
    id="variant-fix",
    description="d",
    target="the thing",
    expected_change="it works",
    undo_notes="undo it",
    commands=("t.step1", "t.step2"),
    resolves_rule="t.rule",
    affected_collectors=("system",),
)
CONFIRM = {"apply": True, "confirmation": "variant-fix"}


class World:
    def __init__(self) -> None:
        self.broken = True
        self.fixed_by: str | None = "t.step2"

    def rental(self, ctx: HostContext) -> RentalAssessment:
        none = RentalState.NONE
        return RentalAssessment(
            none, (SourceReading("containers", none, "t", T0),), T0, "t"
        )

    def observe(self, fix: Fix) -> list[Fact]:
        return [Fact(collector="t", key="t.broken", value=self.broken)]

    def on_run(self, op_id: str) -> None:
        if op_id == self.fixed_by:
            self.broken = False


def build(
    tmp_path: Path,
    fix: Fix,
    world: World | None = None,
    runner: FakeMutationRunner | None = None,
):
    world = world or World()
    ops = {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"], "t.set": ["/s"]}
    runner = runner or FakeMutationRunner(ops, on_run=world.on_run)
    slept: list[float] = []
    engine = FixEngine(
        {fix.id: fix},
        runner,
        fake_context(),
        RULES,
        log=ActionLog(tmp_path / "actions.jsonl"),
        lock_path=tmp_path / "fix.lock",
        rental_check=world.rental,
        observe=world.observe,
        clock=lambda: T0,
        sleep=slept.append,
    )
    return engine, runner, world, slept


def variant(**changes: object) -> Fix:
    return Fix(**{**BASE.__dict__, **changes})


# -- demo only ------------------------------------------------------------------------------


def test_a_demo_only_fix_is_refused_unless_the_operator_says_so(tmp_path: Path) -> None:
    engine, runner, _, _ = build(tmp_path, variant(demo_only=True))
    assert engine.run("variant-fix", **CONFIRM).block_code == "demo_only"
    assert runner.run_calls == []
    assert (
        engine.run("variant-fix", allow_demo=True, **CONFIRM).status
        is FixStatus.SUCCEEDED
    )


def test_a_demo_only_dry_run_is_refused_too_so_it_is_never_offered_on_a_live_host(
    tmp_path: Path,
) -> None:
    engine, _, _, _ = build(tmp_path, variant(demo_only=True))
    assert engine.run("variant-fix").block_code == "demo_only"


# -- preconditions ----------------------------------------------------------------------------


def test_a_failing_precheck_blocks_before_a_dry_run_is_shown(tmp_path: Path) -> None:
    engine, runner, _, _ = build(
        tmp_path, variant(precheck=lambda facts: "not a setup we understand")
    )
    outcome = engine.run("variant-fix")
    assert outcome.block_code == "precondition_failed"
    assert "not a setup we understand" in outcome.reasons[0]
    assert runner.run_calls == []


def test_a_precheck_that_fails_after_confirmation_blocks_the_change(
    tmp_path: Path,
) -> None:
    calls = {"n": 0}

    def precheck(facts: object) -> str | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else "the setup changed"

    engine, runner, _, _ = build(tmp_path, variant(precheck=precheck))
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.block_code == "state_changed"
    assert "the setup changed" in outcome.reasons[0]
    assert runner.run_calls == []


# -- operands (what the fix acts on) -------------------------------------------------------------


def test_a_target_that_cannot_be_worked_out_blocks_the_fix(tmp_path: Path) -> None:
    engine, runner, _, _ = build(tmp_path, variant(operand_source=lambda ctx: None))
    assert engine.run("variant-fix").block_code == "operands_unavailable"
    assert runner.run_calls == []


def test_operands_reach_only_the_steps_that_name_them(tmp_path: Path) -> None:
    fix = variant(
        commands=(FixStep("t.set", ("link",)), FixStep("t.step1")),
        operand_source=lambda ctx: {"link": "eth0"},
    )
    engine, runner, _, _ = build(tmp_path, fix)
    engine.run("variant-fix", **CONFIRM)
    assert runner.run_calls == ["t.set", "t.step1"]
    assert runner.run_operands == [{"link": "eth0"}, {}]


def test_a_step_naming_a_missing_operand_blocks(tmp_path: Path) -> None:
    fix = variant(
        commands=(FixStep("t.set", ("link",)),),
        operand_source=lambda ctx: {"other": "x"},
    )
    engine, runner, _, _ = build(tmp_path, fix)
    assert engine.run("variant-fix").block_code == "operands_unavailable"
    assert runner.run_calls == []


# -- settle time -------------------------------------------------------------------------------------


def test_the_engine_waits_for_the_settle_time_before_observing(tmp_path: Path) -> None:
    engine, _, _, slept = build(tmp_path, variant(settle_seconds=7.0))
    engine.run("variant-fix", **CONFIRM)
    assert slept == [7.0]


def test_nothing_waits_when_blocked_or_only_showing(tmp_path: Path) -> None:
    engine, _, _, slept = build(tmp_path, variant(settle_seconds=7.0))
    engine.run("variant-fix")
    engine.run("variant-fix", apply=True, confirmation="wrong")
    assert slept == []


def test_a_service_that_crashes_during_the_settle_time_is_not_reported_as_fixed(
    tmp_path: Path,
) -> None:
    engine, _, world, _ = build(tmp_path, variant(settle_seconds=5.0))
    engine._sleep = lambda seconds: setattr(world, "broken", True)  # type: ignore[attr-defined]
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.FAILED
    assert "still present" in outcome.reasons[0]


# -- rollback -------------------------------------------------------------------------------------------


def log_lines(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "actions.jsonl").read_text().splitlines()
    ]


def test_rollback_runs_when_the_fix_does_not_work_and_is_logged(tmp_path: Path) -> None:
    world = World()
    world.fixed_by = None  # the commands succeed but change nothing
    engine, runner, _, _ = build(
        tmp_path, variant(rollback=(FixStep("t.undo"),)), world
    )
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.FAILED
    assert outcome.rolled_back
    assert outcome.reasons[-1] == "rolled back"
    assert runner.run_calls == ["t.step1", "t.step2", "t.undo"]
    lines = log_lines(tmp_path)
    assert [line["phase"] for line in lines].count("rollback") == 1
    assert lines[-1]["rolled_back"] is True


def test_rollback_also_runs_when_a_command_fails(tmp_path: Path) -> None:
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]},
        results={
            "t.step2": CommandResult("t.step2", CommandStatus.ERROR, returncode=1)
        },
    )
    engine, runner, _, _ = build(
        tmp_path, variant(rollback=(FixStep("t.undo"),)), runner=runner
    )
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.FAILED and outcome.rolled_back
    # step 1 changed something, step 2 failed, so the undo ran
    assert runner.run_calls == ["t.step1", "t.step2", "t.undo"]


def test_no_rollback_when_the_first_command_failed_and_nothing_changed(
    tmp_path: Path,
) -> None:
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]},
        results={
            "t.step1": CommandResult("t.step1", CommandStatus.ERROR, returncode=1)
        },
    )
    engine, runner, _, _ = build(
        tmp_path, variant(rollback=(FixStep("t.undo"),)), runner=runner
    )
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.FAILED
    assert not outcome.rolled_back  # there was nothing to undo
    assert runner.run_calls == ["t.step1"]


def test_a_successful_fix_is_never_rolled_back(tmp_path: Path) -> None:
    engine, runner, _, _ = build(tmp_path, variant(rollback=(FixStep("t.undo"),)))
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.SUCCEEDED and not outcome.rolled_back
    assert "t.undo" not in runner.run_calls


def test_a_fix_without_a_rollback_reports_none_was_done(tmp_path: Path) -> None:
    world = World()
    world.fixed_by = None
    engine, _, _, _ = build(tmp_path, variant(), world)
    assert not engine.run("variant-fix", **CONFIRM).rolled_back


# -- warnings and validation --------------------------------------------------------------------------------


def test_warnings_are_shown_at_dry_run_and_when_blocked(tmp_path: Path) -> None:
    engine, _, _, _ = build(tmp_path, variant(warnings=("keep a console open",)))
    assert engine.run("variant-fix").warnings == ("keep a console open",)
    assert engine.run("variant-fix", apply=True, confirmation="x").warnings == (
        "keep a console open",
    )


def test_fix_steps_accept_plain_strings_and_settle_time_is_bounded() -> None:
    assert BASE.commands == (FixStep("t.step1"), FixStep("t.step2"))
    with pytest.raises(ValueError, match="settle_seconds"):
        variant(settle_seconds=500.0)


# -- a failed undo must never be reported as a successful one ------------------------------------
# Found by independent testing (TestingDoneByCodex.md): the engine ignored the result of the
# rollback steps and always said "rolled back".


def failing_undo_engine(tmp_path: Path, status: CommandStatus = CommandStatus.ERROR):
    world = World()
    world.fixed_by = None  # the fix does not work, so a rollback is attempted
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]},
        results={"t.undo": CommandResult("t.undo", status, returncode=7)},
        on_run=world.on_run,
    )
    return build(tmp_path, variant(rollback=(FixStep("t.undo"),)), world, runner)[0]


def test_a_failed_undo_is_never_reported_as_rolled_back(tmp_path: Path) -> None:
    outcome = failing_undo_engine(tmp_path).run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.FAILED
    assert outcome.rolled_back is False
    assert outcome.rollback_failed is True
    assert "rolled back" not in outcome.reasons
    assert "ROLLBACK FAILED" in outcome.reasons[-1]
    assert "t.undo failed" in outcome.reasons[-1]
    assert "may still be in place" in outcome.reasons[-1]


def test_a_timed_out_undo_is_also_a_failed_undo(tmp_path: Path) -> None:
    outcome = failing_undo_engine(tmp_path, CommandStatus.TIMEOUT).run(
        "variant-fix", **CONFIRM
    )
    assert outcome.rollback_failed is True and outcome.rolled_back is False
    assert "t.undo timed out" in outcome.reasons[-1]


def test_a_failed_undo_is_recorded_honestly_in_the_action_log(tmp_path: Path) -> None:
    failing_undo_engine(tmp_path).run("variant-fix", **CONFIRM)
    lines = log_lines(tmp_path)
    undo = [line for line in lines if line["phase"] == "rollback"]
    assert undo and undo[0]["status"] == "error" and undo[0]["returncode"] == 7
    assert lines[-1]["rolled_back"] is False
    assert lines[-1]["rollback_failed"] is True
    assert "ROLLBACK FAILED" in lines[-1]["reasons"][-1]


def test_a_working_undo_is_still_reported_as_rolled_back(tmp_path: Path) -> None:
    world = World()
    world.fixed_by = None
    engine, _, _, _ = build(tmp_path, variant(rollback=(FixStep("t.undo"),)), world)
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.rolled_back is True and outcome.rollback_failed is False
    assert outcome.reasons[-1] == "rolled back"


def test_a_failure_with_no_rollback_defined_claims_neither(tmp_path: Path) -> None:
    world = World()
    world.fixed_by = None
    engine, _, _, _ = build(tmp_path, variant(), world)
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.rolled_back is False and outcome.rollback_failed is False


def test_the_cli_says_the_rollback_failed_and_never_says_rolled_back(
    tmp_path: Path,
) -> None:
    import io

    from rich.console import Console

    from vast_troubleshooter.cli import render_fix

    outcome = failing_undo_engine(tmp_path).run("variant-fix", **CONFIRM)
    buffer = io.StringIO()
    render_fix(outcome, Console(file=buffer, width=200, highlight=False))
    text = buffer.getvalue()
    assert "ROLLBACK FAILED" in text
    assert "the change was rolled back" not in text
    assert "Check the system by hand" in text


def test_the_report_carries_the_failed_rollback_flag() -> None:
    from vast_troubleshooter.report import ReportAction, _action

    action = _action(
        {
            "run_id": "r",
            "phase": "apply_finished",
            "rolled_back": False,
            "rollback_failed": True,
        }
    )
    assert isinstance(action, ReportAction) and action.rollback_failed is True


# -- found by independent testing (F-04, F-05, F-06) ----------------------------------------------

from vast_troubleshooter.fixes.base import OperandsUnavailable


class OperandAwareRunner(FakeMutationRunner):
    """Like the real runner, the planned command includes the operand."""

    def plan(self, op_id: str, **operands: str):  # type: ignore[no-untyped-def]
        argv, early = super().plan(op_id)
        return [*argv, *operands.values()], early


def test_apply_is_blocked_if_the_target_changed_since_the_preview(
    tmp_path: Path,
) -> None:
    """F-04: the operator saw eth0; a second route made the tool pick eth1."""
    link = {"link": "eth0"}
    fix = variant(
        commands=(FixStep("t.set", ("link",)),),
        operand_source=lambda ctx: dict(link),
    )
    world = World()
    runner = OperandAwareRunner(
        {"t.set": ["/s"], "t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]},
        on_run=world.on_run,
    )
    engine, runner, _, _ = build(tmp_path, fix, world, runner)
    preview = engine.run("variant-fix")
    assert preview.commands == (("/s", "eth0"),)

    link["link"] = "eth1"  # the host changed while the operator was reading
    outcome = engine.run("variant-fix", expected_commands=preview.commands, **CONFIRM)
    assert outcome.block_code == "target_changed"
    assert runner.run_calls == []  # nothing ran against eth1


def test_apply_runs_when_the_target_is_what_was_previewed(tmp_path: Path) -> None:
    fix = variant(
        commands=(FixStep("t.set", ("link",)), FixStep("t.step2")),
        operand_source=lambda ctx: {"link": "eth0"},
    )
    engine, _, _, _ = build(tmp_path, fix)
    preview = engine.run("variant-fix")
    outcome = engine.run("variant-fix", expected_commands=preview.commands, **CONFIRM)
    assert outcome.status is FixStatus.SUCCEEDED


def test_an_operand_source_can_refuse_with_its_own_reason(tmp_path: Path) -> None:
    def refuse(ctx: HostContext) -> dict[str, str]:
        raise OperandsUnavailable("eth0 already has its own DNS servers")

    engine, runner, _, _ = build(tmp_path, variant(operand_source=refuse))
    outcome = engine.run("variant-fix")
    assert outcome.block_code == "operands_unavailable"
    assert "already has its own DNS servers" in outcome.reasons[0]
    assert runner.run_calls == []


class FlakyLog(ActionLog):
    """Writes fine until the change has started, then the disk 'fills up'."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.writes = 0

    def write(self, record) -> None:  # type: ignore[no-untyped-def]
        self.writes += 1
        if self.writes > 1:  # the first write is `apply_started`
            raise OSError(28, "No space left on device")
        super().write(record)


def test_a_log_failure_after_the_change_is_reported_not_raised(tmp_path: Path) -> None:
    """F-06: it used to escape as an uncaught OSError after the system had changed."""
    world = World()
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"]}, on_run=world.on_run
    )
    engine = FixEngine(
        {BASE.id: BASE},
        runner,
        fake_context(),
        RULES,
        log=FlakyLog(tmp_path / "actions.jsonl"),
        lock_path=tmp_path / "fix.lock",
        rental_check=world.rental,
        observe=world.observe,
        clock=lambda: T0,
        sleep=lambda s: None,
    )
    outcome = engine.run("variant-fix", **CONFIRM)  # must not raise
    assert outcome.status is FixStatus.SUCCEEDED  # the fix did work
    assert any("ACTION LOG COULD NOT BE WRITTEN" in r for r in outcome.reasons)


def test_a_successful_fix_is_not_rolled_back_because_a_later_step_misreported(
    tmp_path: Path,
) -> None:
    """F-05(a): the check passes, but the last command exited non-zero."""
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]},
        results={
            "t.step2": CommandResult("t.step2", CommandStatus.ERROR, returncode=1)
        },
        on_run=World().on_run,
    )
    world = World()
    world.fixed_by = "t.step1"  # the first command fixes it; the second then fails
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]},
        results={
            "t.step2": CommandResult("t.step2", CommandStatus.ERROR, returncode=1)
        },
        on_run=world.on_run,
    )
    engine, runner, _, _ = build(
        tmp_path, variant(rollback=(FixStep("t.undo"),)), world, runner
    )
    outcome = engine.run("variant-fix", **CONFIRM)
    assert not outcome.rolled_back
    assert "t.undo" not in runner.run_calls  # a working fix is left alone


# -- planning covers the undo too, and a missing tool is named as such (F-18, F-19) ---------------


class MissingToolRunner(FakeMutationRunner):
    def __init__(self, *args, missing: set[str], **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._missing = missing

    def plan(self, op_id: str, **operands: str):  # type: ignore[no-untyped-def]
        if op_id in self._missing:
            return [], CommandResult(
                op_id, CommandStatus.MISSING, detail="resolvectl was not found"
            )
        return super().plan(op_id, **operands)


def test_a_missing_tool_is_reported_as_missing_not_as_not_allowed(
    tmp_path: Path,
) -> None:
    runner = MissingToolRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"]}, missing={"t.step2"}
    )
    engine, runner, _, _ = build(tmp_path, BASE, runner=runner)
    outcome = engine.run("variant-fix")
    assert outcome.block_code == "tool_missing"
    assert "was not found" in outcome.reasons[0]


def test_a_fix_whose_undo_cannot_run_is_not_started(tmp_path: Path) -> None:
    runner = MissingToolRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"], "t.undo": ["/u"]}, missing={"t.undo"}
    )
    engine, runner, _, _ = build(
        tmp_path, variant(rollback=(FixStep("t.undo"),)), runner=runner
    )
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.block_code == "rollback_tool_missing"
    assert "undo" in outcome.reasons[0]
    assert runner.run_calls == []  # the fix itself never ran
