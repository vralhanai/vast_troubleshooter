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
            "t.step1": CommandResult("t.step1", CommandStatus.ERROR, returncode=1)
        },
    )
    engine, runner, _, _ = build(
        tmp_path, variant(rollback=(FixStep("t.undo"),)), runner=runner
    )
    outcome = engine.run("variant-fix", **CONFIRM)
    assert outcome.status is FixStatus.FAILED and outcome.rolled_back
    assert runner.run_calls == ["t.step1", "t.undo"]  # step 2 skipped, undo ran


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
