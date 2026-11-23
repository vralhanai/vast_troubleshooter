import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.fixes import (
    FIXES,
    MUTATION_OPERATIONS,
    ActionLog,
    Fix,
    FixEngine,
    FixStatus,
    LockBusyError,
    ProcessLock,
)
from vast_troubleshooter.models import Fact
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.rentals import (
    RentalAssessment,
    RentalState,
    SourceReading,
)
from vast_troubleshooter.rule_engine import load_rules_text
from vast_troubleshooter.runner import (
    CommandResult,
    CommandStatus,
    Operation,
    SubprocessRunner,
)
from vast_troubleshooter.testing import FakeMutationRunner, fake_context

T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
A, N, U = RentalState.ACTIVE, RentalState.NONE, RentalState.UNKNOWN

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

FIX = Fix(
    id="test-fix",
    description="Fixes the thing",
    target="the test service",
    expected_change="t.broken becomes false",
    undo_notes="Nothing to undo in a test.",
    commands=("t.step1", "t.step2"),
    resolves_rule="t.rule",
    affected_collectors=("system",),
)


class World:
    """A pretend host: `broken` is the problem, `rentals` is what the rental
    check answers on each call (the last answer repeats)."""

    def __init__(
        self, broken: bool | None = True, rentals: tuple[RentalState, ...] = (N,)
    ) -> None:
        self.broken = broken
        self.rentals = list(rentals)
        self.rental_calls = 0
        self.observe_calls = 0
        self.fixed_by: str | None = "t.step2"  # the command that fixes it

    def rental_check(self, ctx: HostContext) -> RentalAssessment:
        state = self.rentals[min(self.rental_calls, len(self.rentals) - 1)]
        self.rental_calls += 1
        return RentalAssessment(
            state,
            (SourceReading("containers", state, "test", T0),),
            T0,
            f"test says {state.value}",
        )

    def observe(self, fix: Fix) -> list[Fact]:
        self.observe_calls += 1
        if self.broken is None:
            return []
        return [Fact(collector="t", key="t.broken", value=self.broken)]

    def on_run(self, op_id: str) -> None:
        if op_id == self.fixed_by:
            self.broken = False


def make(
    world: World,
    tmp_path: Path,
    fix: Fix = FIX,
    runner: FakeMutationRunner | None = None,
    clock_offset: timedelta = timedelta(0),
) -> tuple[FixEngine, FakeMutationRunner, ActionLog]:
    runner = runner or FakeMutationRunner(
        {"t.step1": ["/usr/bin/step1"], "t.step2": ["/usr/bin/step2", "--now"]},
        on_run=world.on_run,
    )
    log = ActionLog(tmp_path / "actions.jsonl")
    engine = FixEngine(
        {fix.id: fix},
        runner,
        fake_context(),
        RULES,
        log=log,
        lock_path=tmp_path / "fix.lock",
        rental_check=world.rental_check,
        observe=world.observe,
        clock=lambda: T0 + clock_offset,
    )
    return engine, runner, log


def apply(
    engine: FixEngine, fix_id: str = "test-fix", confirmation: str | None = "test-fix"
):
    return engine.run(fix_id, apply=True, confirmation=confirmation)


# -- dry run is the default and changes nothing --------------------------------------


def test_dry_run_is_the_default_and_changes_nothing(tmp_path: Path) -> None:
    world = World()
    engine, runner, log = make(world, tmp_path)
    outcome = engine.run("test-fix")
    assert outcome.status is FixStatus.DRY_RUN
    assert runner.run_calls == []
    assert world.broken is True
    assert not (tmp_path / "fix.lock").exists()  # no lock is needed to look
    assert outcome.commands == (("/usr/bin/step1",), ("/usr/bin/step2", "--now"))
    assert outcome.target == "the test service"
    assert outcome.expected_change == "t.broken becomes false"
    assert outcome.undo_notes == "Nothing to undo in a test."
    assert outcome.before == "fail"
    assert [e["phase"] for e in log.entries] == ["dry_run"]


def test_dry_run_does_not_need_a_confirmation_and_ignores_one(tmp_path: Path) -> None:
    engine, runner, _ = make(World(), tmp_path)
    assert engine.run("test-fix", confirmation="test-fix").status is FixStatus.DRY_RUN
    assert runner.run_calls == []


# -- the checks, in order -------------------------------------------------------------


def test_unknown_fix_is_blocked(tmp_path: Path) -> None:
    engine, runner, _ = make(World(), tmp_path)
    outcome = apply(engine, "restart-everything", "restart-everything")
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.block_code == "unknown_fix"
    assert runner.run_calls == []


def test_needs_root_blocks_before_anything_else(tmp_path: Path) -> None:
    world = World(rentals=(A,))
    runner = FakeMutationRunner(
        {"t.step1": ["/x"], "t.step2": ["/y"]}, needs_root=frozenset({"t.step2"})
    )
    engine, runner, _ = make(world, tmp_path, runner=runner)
    outcome = apply(engine)
    assert outcome.block_code == "needs_root"
    assert runner.run_calls == []
    assert world.rental_calls == 0  # root is checked before the rental


@pytest.mark.parametrize(
    ("state", "code"), [(A, "rental_active"), (U, "rental_unknown")]
)
def test_active_or_unknown_rental_blocks_even_with_the_right_confirmation(
    tmp_path: Path, state: RentalState, code: str
) -> None:
    world = World(rentals=(state,))
    engine, runner, _ = make(world, tmp_path)
    outcome = apply(engine)
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.block_code == code
    assert outcome.rental_state == state.value
    assert runner.run_calls == []
    assert world.broken is True


def test_a_fix_that_does_not_need_the_rental_check_skips_it(tmp_path: Path) -> None:
    fix = Fix(**{**FIX.__dict__, "id": "safe-fix", "requires_no_active_rental": False})
    world = World(rentals=(A,))
    engine, _, _ = make(world, tmp_path, fix=fix)
    assert apply(engine, "safe-fix", "safe-fix").status is FixStatus.SUCCEEDED
    assert world.rental_calls == 0


def test_the_whole_default_with_the_real_default_rental_check_is_blocked(
    tmp_path: Path,
) -> None:
    """With the real rental check the VM stub makes the state unknown: fixes are blocked."""
    runner = FakeMutationRunner({"t.step1": ["/x"], "t.step2": ["/y"]})
    engine = FixEngine(
        {FIX.id: FIX},
        runner,
        fake_context(),
        RULES,
        log=ActionLog(tmp_path / "a.jsonl"),
        lock_path=tmp_path / "l",
        observe=World().observe,
    )
    outcome = apply(engine)
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.block_code == "rental_unknown"
    assert runner.run_calls == []


@pytest.mark.parametrize(
    ("broken", "code_text"),
    [(False, "already passes"), (None, "cannot confirm the problem exists")],
)
def test_no_fix_when_the_problem_is_absent_or_unknown(
    tmp_path: Path, broken: bool | None, code_text: str
) -> None:
    engine, runner, _ = make(World(broken=broken), tmp_path)
    outcome = apply(engine)
    assert outcome.block_code == "no_problem"
    assert code_text in outcome.reasons[0]
    assert runner.run_calls == []


# -- confirmation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed", [None, "", "yes", "y", "TEST-FIX", " test-fix", "test-fix ", "test"]
)
def test_confirmation_mismatch_changes_nothing(
    tmp_path: Path, typed: str | None
) -> None:
    world = World()
    engine, runner, log = make(world, tmp_path)
    outcome = apply(engine, confirmation=typed)
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.block_code == "confirmation_mismatch"
    assert runner.run_calls == []
    assert world.broken is True
    assert not (tmp_path / "fix.lock").exists()
    assert "apply_started" not in [e["phase"] for e in log.entries]


# -- a successful run ---------------------------------------------------------------------


def test_apply_runs_every_command_in_order_and_confirms_the_check_passes(
    tmp_path: Path,
) -> None:
    world = World()
    engine, runner, log = make(world, tmp_path)
    outcome = apply(engine)
    assert outcome.status is FixStatus.SUCCEEDED
    assert runner.run_calls == ["t.step1", "t.step2"]
    assert (outcome.before, outcome.after) == ("fail", "pass")
    assert outcome.undo_notes
    assert [e["phase"] for e in log.entries] == [
        "apply_started",
        "command",
        "command",
        "apply_finished",
    ]


def test_rental_is_checked_again_immediately_before_the_change(tmp_path: Path) -> None:
    world = World()
    engine, _, _ = make(world, tmp_path)
    apply(engine)
    assert world.rental_calls == 2  # once to decide, once right before changing


def test_the_collectors_are_observed_before_and_again_before_and_after(
    tmp_path: Path,
) -> None:
    world = World()
    engine, _, _ = make(world, tmp_path)
    apply(engine)
    assert world.observe_calls == 3  # decide, re-check before change, observe after


# -- the host changes while the operator confirms ------------------------------------------


def test_a_rental_that_appears_after_confirmation_blocks_the_change(
    tmp_path: Path,
) -> None:
    world = World(rentals=(N, A))  # none at first, active at the second check
    engine, runner, _ = make(world, tmp_path)
    outcome = apply(engine)
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.block_code == "state_changed"
    assert "active" in outcome.reasons[0]
    assert runner.run_calls == []
    assert world.broken is True


def test_a_rental_that_becomes_unknown_after_confirmation_blocks_the_change(
    tmp_path: Path,
) -> None:
    engine, runner, _ = make(World(rentals=(N, U)), tmp_path)
    assert apply(engine).block_code == "state_changed"
    assert runner.run_calls == []


def test_the_problem_disappearing_after_confirmation_blocks_the_change(
    tmp_path: Path,
) -> None:
    world = World()
    engine, runner, _ = make(world, tmp_path)
    original = world.observe

    def flips(fix: Fix) -> list[Fact]:
        facts = original(fix)
        if world.observe_calls == 2:  # the re-check right before the change
            world.broken = False
        return (
            facts
            if world.observe_calls != 2
            else [Fact(collector="t", key="t.broken", value=False)]
        )

    engine._observe = flips  # type: ignore[attr-defined]
    outcome = apply(engine)
    assert outcome.block_code == "state_changed"
    assert runner.run_calls == []


def test_stale_none_evidence_does_not_authorise_a_change(tmp_path: Path) -> None:
    world = World()

    def old_none(ctx: HostContext) -> RentalAssessment:
        return RentalAssessment(
            N,
            (SourceReading("containers", N, "t", T0 - timedelta(minutes=5)),),
            T0,
            "old",
        )

    engine, runner, _ = make(world, tmp_path)
    engine._rental_check = old_none  # type: ignore[attr-defined]
    outcome = apply(engine)
    assert outcome.block_code == "rental_unknown"
    assert runner.run_calls == []


# -- failures and timeouts are judged by what is observed -------------------------------------


def test_a_failing_command_stops_the_run_and_is_reported(tmp_path: Path) -> None:
    world = World()
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"]},
        results={
            "t.step1": CommandResult("t.step1", CommandStatus.ERROR, returncode=1)
        },
        on_run=world.on_run,
    )
    engine, runner, _ = make(world, tmp_path, runner=runner)
    outcome = apply(engine)
    assert outcome.status is FixStatus.FAILED
    assert runner.run_calls == ["t.step1"]  # step 2 never ran
    assert "t.step1 failed" in outcome.reasons[0]
    assert outcome.after == "fail"


def test_the_problem_still_present_after_a_clean_run_is_a_failure(
    tmp_path: Path,
) -> None:
    world = World()
    world.fixed_by = None  # commands succeed but change nothing
    engine, _, _ = make(world, tmp_path)
    outcome = apply(engine)
    assert outcome.status is FixStatus.FAILED
    assert "still present" in outcome.reasons[0]


@pytest.mark.parametrize(
    ("after_broken", "expected"),
    [
        (False, FixStatus.SUCCEEDED),
        (True, FixStatus.FAILED),
        (None, FixStatus.INDETERMINATE),
    ],
)
def test_a_timeout_is_judged_by_what_is_observed_afterwards(
    tmp_path: Path, after_broken: bool | None, expected: FixStatus
) -> None:
    world = World()

    def on_run(op_id: str) -> None:
        world.broken = after_broken

    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"]},
        results={"t.step1": CommandResult("t.step1", CommandStatus.TIMEOUT)},
        on_run=on_run,
    )
    engine, _, _ = make(world, tmp_path, runner=runner)
    outcome = apply(engine)
    assert outcome.status is expected
    assert "timed out" in outcome.reasons[0] or expected is FixStatus.SUCCEEDED


def test_an_unobservable_result_is_indeterminate_not_success(tmp_path: Path) -> None:
    world = World()

    def on_run(op_id: str) -> None:
        world.broken = None  # the collector can no longer see the thing

    runner = FakeMutationRunner({"t.step1": ["/a"], "t.step2": ["/b"]}, on_run=on_run)
    engine, _, _ = make(world, tmp_path, runner=runner)
    outcome = apply(engine)
    assert outcome.status is FixStatus.INDETERMINATE
    assert "cannot be told" in outcome.reasons[0]


def test_a_fix_without_a_check_succeeds_only_if_its_commands_do(tmp_path: Path) -> None:
    fix = Fix(**{**FIX.__dict__, "id": "no-check", "resolves_rule": None})
    world = World()
    engine, _, _ = make(world, tmp_path, fix=fix)
    outcome = apply(engine, "no-check", "no-check")
    assert outcome.status is FixStatus.SUCCEEDED
    assert "no check" in outcome.reasons[0]


# -- concurrency -------------------------------------------------------------------------------


def test_a_second_run_is_blocked_while_the_lock_is_held(tmp_path: Path) -> None:
    world = World()
    engine, runner, _ = make(world, tmp_path)
    with ProcessLock(tmp_path / "fix.lock"):
        outcome = apply(engine)
        assert outcome.status is FixStatus.BLOCKED
        assert outcome.block_code == "locked"
        assert runner.run_calls == []
    assert apply(engine).status is FixStatus.SUCCEEDED  # released: now it runs


def test_the_lock_is_released_after_a_run_and_after_a_crash(tmp_path: Path) -> None:
    world = World()

    def explode(op_id: str) -> None:
        raise RuntimeError("boom")

    runner = FakeMutationRunner({"t.step1": ["/a"], "t.step2": ["/b"]}, on_run=explode)
    engine, _, _ = make(world, tmp_path, runner=runner)
    with pytest.raises(RuntimeError):
        apply(engine)
    lock = ProcessLock(tmp_path / "fix.lock")
    lock.acquire()  # would raise if the crash left it held
    lock.release()


def test_two_locks_on_one_file_conflict(tmp_path: Path) -> None:
    first, second = ProcessLock(tmp_path / "x.lock"), ProcessLock(tmp_path / "x.lock")
    first.acquire()
    with pytest.raises(LockBusyError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


# -- the action log -----------------------------------------------------------------------------


def test_log_lines_are_json_with_one_run_id_and_no_command_output(
    tmp_path: Path,
) -> None:
    world = World()
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"]},
        results={
            "t.step2": CommandResult(
                "t.step2",
                CommandStatus.OK,
                returncode=0,
                stdout="SECRET-OUTPUT",
                stderr="SECRET-ERR",
            )
        },
        on_run=world.on_run,
    )
    engine, _, _ = make(world, tmp_path, runner=runner)
    apply(engine)
    text = (tmp_path / "actions.jsonl").read_text()
    lines = [json.loads(line) for line in text.splitlines()]
    assert len(lines) == 4
    assert {line["run_id"] for line in lines} == {lines[0]["run_id"]}
    assert all(
        line["schema_version"] == 1 and line["fix_id"] == "test-fix" for line in lines
    )
    assert "SECRET" not in text
    finished = lines[-1]
    assert finished["phase"] == "apply_finished" and finished["undo_notes"]
    assert lines[0]["commands"] == [["/a"], ["/b"]]


def test_blocked_runs_are_logged_with_their_reason(tmp_path: Path) -> None:
    engine, _, log = make(World(rentals=(A,)), tmp_path)
    apply(engine)
    entry = log.entries[-1]
    assert entry["phase"] == "blocked" and entry["block_code"] == "rental_active"
    assert (tmp_path / "actions.jsonl").exists()


def test_no_change_is_made_if_the_log_cannot_be_written(tmp_path: Path) -> None:
    world = World()
    bad = tmp_path / "a-directory"
    bad.mkdir()
    runner = FakeMutationRunner(
        {"t.step1": ["/a"], "t.step2": ["/b"]}, on_run=world.on_run
    )
    engine = FixEngine(
        {FIX.id: FIX},
        runner,
        fake_context(),
        RULES,
        log=ActionLog(bad),
        lock_path=tmp_path / "l",
        rental_check=world.rental_check,
        observe=world.observe,
        clock=lambda: T0,
    )
    outcome = apply(engine)
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.block_code == "log_unwritable"
    assert runner.run_calls == []
    assert world.broken is True


def test_a_dry_run_still_shows_when_the_log_is_unwritable(tmp_path: Path) -> None:
    bad = tmp_path / "a-directory"
    bad.mkdir()
    world = World()
    engine = FixEngine(
        {FIX.id: FIX},
        FakeMutationRunner({"t.step1": ["/a"], "t.step2": ["/b"]}),
        fake_context(),
        RULES,
        log=ActionLog(bad),
        lock_path=tmp_path / "l",
        rental_check=world.rental_check,
        observe=world.observe,
        clock=lambda: T0,
    )
    assert engine.run("test-fix").status is FixStatus.DRY_RUN


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and symlinks")
def test_log_and_lock_are_owner_only_and_never_through_a_symlink(
    tmp_path: Path,
) -> None:
    engine, _, _ = make(World(), tmp_path)
    apply(engine)
    for name in ("actions.jsonl", "fix.lock"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    target = tmp_path / "target.txt"
    target.write_text("keep")
    link = tmp_path / "evil.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        ActionLog(link).write({"x": 1})
    assert target.read_text() == "keep"


# -- fixes and the engine are validated ------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "Bad Id", "rm -rf", "-x", "a" * 61])
def test_a_fix_id_must_be_a_plain_name(bad_id: str) -> None:
    with pytest.raises(ValueError):
        Fix(**{**FIX.__dict__, "id": bad_id})


def test_a_fix_needs_commands_and_a_complete_description() -> None:
    with pytest.raises(ValueError):
        Fix(**{**FIX.__dict__, "commands": ()})
    for field in ("description", "target", "expected_change", "undo_notes"):
        with pytest.raises(ValueError):
            Fix(**{**FIX.__dict__, field: "  "})


def test_the_engine_rejects_a_fix_that_names_an_unknown_rule(tmp_path: Path) -> None:
    fix = Fix(**{**FIX.__dict__, "resolves_rule": "nope.nope"})
    with pytest.raises(ValueError, match="unknown rule"):
        FixEngine(
            {fix.id: fix},
            FakeMutationRunner({}),
            fake_context(),
            RULES,
            log=ActionLog(tmp_path / "a"),
        )


# -- the mutation allowlist is separate and empty ----------------------------------------------------


def test_the_read_and_mutation_registries_are_separate() -> None:
    assert set(FIXES) == {"restart-vast-daemon", "fix-dns-resolver"}
    assert not set(MUTATION_OPERATIONS) & set(READ_OPERATIONS)


def test_the_read_runner_cannot_run_a_mutation_operation() -> None:
    mutation = Operation(id="t.step1", executable="true")
    read_runner = SubprocessRunner(READ_OPERATIONS)
    assert read_runner.run(mutation.id).status is CommandStatus.REFUSED
    mutation_runner = SubprocessRunner({mutation.id: mutation})
    assert mutation_runner.run("system.uname_release").status is CommandStatus.REFUSED


# -- a real subprocess, end to end --------------------------------------------------------------------


def test_end_to_end_with_a_real_subprocess(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    code = f"open({str(marker)!r}, 'w').write('ran')"
    op = Operation(id="t.write", executable=sys.executable, args=("-I", "-c", code))
    fix = Fix(**{**FIX.__dict__, "commands": ("t.write",)})
    world = World()
    world.fixed_by = "t.write"
    runner = SubprocessRunner({op.id: op})
    engine = FixEngine(
        {fix.id: fix},
        _Spy(runner, world.on_run),
        fake_context(),
        RULES,
        log=ActionLog(tmp_path / "actions.jsonl"),
        lock_path=tmp_path / "fix.lock",
        rental_check=world.rental_check,
        observe=world.observe,
        clock=lambda: T0,
    )
    dry = engine.run("test-fix")
    assert (
        dry.status is FixStatus.DRY_RUN and not marker.exists()
    )  # a dry run does nothing
    done = engine.run("test-fix", apply=True, confirmation="test-fix")
    assert done.status is FixStatus.SUCCEEDED
    assert marker.read_text() == "ran"


class _Spy:
    """Wraps a real runner and tells the pretend world when a command ran."""

    def __init__(self, inner: SubprocessRunner, on_run) -> None:
        self._inner, self._on_run = inner, on_run

    def plan(self, op_id: str, **operands: str):
        return self._inner.plan(op_id, **operands)

    def run(self, op_id: str, **operands: str) -> CommandResult:
        result = self._inner.run(op_id, **operands)
        self._on_run(op_id)
        return result
