from datetime import UTC, datetime, timedelta

import pytest

from vast_troubleshooter import cli
from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.rentals import RentalsCollector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.rentals import (
    MAX_EVIDENCE_AGE,
    REQUIRED_SOURCES,
    ContainersSource,
    RentalState,
    SourceReading,
    SourceResult,
    VmsSource,
    assess,
    blocks_disruption,
    check_rentals,
    default_sources,
)
from vast_troubleshooter.runner import (
    TRUSTED_DOCKER_ENDPOINT,
    CommandResult,
    CommandStatus,
    SubprocessRunner,
)
from vast_troubleshooter.testing import fake_context

OP = "rentals.docker_ps_all"
T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
A, N, U = RentalState.ACTIVE, RentalState.NONE, RentalState.UNKNOWN


class Fixed:
    """A source that returns a fixed result."""

    def __init__(self, name: str, state: RentalState, reason: str = "") -> None:
        self.name = name
        self._result = SourceResult(state, reason or state.value)

    def read(self, ctx: HostContext) -> SourceResult:
        return self._result


class Exploding:
    name = "containers"

    def read(self, ctx: HostContext) -> SourceResult:
        raise RuntimeError("boom")


def docker(
    stdout: str = "", status: CommandStatus = CommandStatus.OK, **kw
) -> HostContext:
    return fake_context(
        results={OP: CommandResult(OP, status, returncode=0, stdout=stdout, **kw)}
    )


def containers(ctx: HostContext) -> SourceResult:
    return ContainersSource().read(ctx)


def state_with(*sources: Fixed) -> RentalState:
    return check_rentals(fake_context(), sources=sources, clock=lambda: T0).state


# -- containers: every state is a possible rental ----------------------------------


def test_no_containers_means_none_for_this_source() -> None:
    result = containers(docker(""))
    assert result.state is N
    assert result.detail == {"total": 0, "running": 0}


def test_a_running_container_is_active() -> None:
    result = containers(docker("running\n"))
    assert result.state is A
    assert result.detail == {"total": 1, "running": 1}


@pytest.mark.parametrize("state", ["exited", "created", "dead", "paused", "restarting"])
def test_a_stopped_but_allocated_container_is_still_active(state: str) -> None:
    result = containers(docker(f"{state}\n"))
    assert result.state is A
    assert result.detail == {"total": 1, "running": 0}


def test_counts_mixed_states() -> None:
    assert containers(docker("running\nexited\nrunning\n")).detail == {
        "total": 3,
        "running": 2,
    }


@pytest.mark.parametrize(
    "status",
    [
        CommandStatus.MISSING,  # docker not installed
        CommandStatus.TIMEOUT,  # hung
        CommandStatus.REFUSED,  # DOCKER_HOST points elsewhere
        CommandStatus.NEEDS_ROOT,
        CommandStatus.ERROR,  # daemon down or permission denied
    ],
)
def test_a_source_that_cannot_look_is_unknown_never_none(status: CommandStatus) -> None:
    assert containers(docker("", status)).state is U


def test_truncated_output_is_unknown() -> None:
    assert containers(docker("running\n", truncated=True)).state is U


def test_permission_denied_on_the_docker_socket_is_unknown() -> None:
    ctx = fake_context(
        results={
            OP: CommandResult(
                OP,
                CommandStatus.ERROR,
                returncode=1,
                stderr="permission denied while trying to connect to the Docker daemon socket",
            )
        }
    )
    assert containers(ctx).state is U


# -- the VM source is a stub: it can never prove absence ----------------------------


def test_vm_source_is_always_unknown() -> None:
    result = VmsSource().read(fake_context())
    assert result.state is U
    assert "not implemented" in result.reason


def test_with_the_default_sources_none_is_impossible_today() -> None:
    ctx = docker("")  # no containers at all
    assessment = check_rentals(ctx, clock=lambda: T0)
    assert assessment.state is U
    assert assessment.blocks_disruption
    assert assessment.unresolved == ["vms"]
    assert [s.name for s in default_sources()] == list(REQUIRED_SOURCES)


# -- combining sources ---------------------------------------------------------------


def test_none_needs_every_required_source_to_say_none() -> None:
    assert state_with(Fixed("containers", N), Fixed("vms", N)) is N


def test_vm_only_activity_is_active() -> None:
    assert state_with(Fixed("containers", N), Fixed("vms", A)) is A


def test_positive_evidence_beats_both_absence_and_unknown() -> None:
    assert (
        state_with(Fixed("containers", A), Fixed("vms", N)) is A
    )  # conflicting sources
    assert state_with(Fixed("containers", A), Fixed("vms", U)) is A
    assert state_with(Fixed("containers", U), Fixed("vms", A)) is A


def test_one_unknown_source_makes_the_whole_state_unknown() -> None:
    assert state_with(Fixed("containers", N), Fixed("vms", U)) is U
    assert state_with(Fixed("containers", U), Fixed("vms", N)) is U


def test_a_required_source_that_did_not_report_is_unknown() -> None:
    assert state_with(Fixed("containers", N)) is U
    assert state_with() is U


def test_an_unrequired_extra_source_can_still_make_it_active() -> None:
    assert (
        state_with(Fixed("containers", N), Fixed("vms", N), Fixed("daemon_log", A)) is A
    )


def test_a_crashing_source_fails_closed_instead_of_raising() -> None:
    assessment = check_rentals(
        fake_context(), sources=[Exploding(), Fixed("vms", N)], clock=lambda: T0
    )
    assert assessment.state is U
    assert "RuntimeError" in assessment.reason
    assert assessment.blocks_disruption


def test_only_none_allows_a_disruptive_action() -> None:
    assert blocks_disruption(A) and blocks_disruption(U)
    assert not blocks_disruption(N)


# -- freshness: a stale `none` must not authorise a change ------------------------------


def none_assessment() -> object:
    return check_rentals(
        fake_context(),
        sources=[Fixed("containers", N), Fixed("vms", N)],
        clock=lambda: T0,
    )


def test_fresh_none_stays_none() -> None:
    a = none_assessment()
    assert a.state_at(T0) is N  # type: ignore[attr-defined]
    assert a.state_at(T0 + MAX_EVIDENCE_AGE) is N  # type: ignore[attr-defined]


def test_stale_none_is_downgraded_to_unknown() -> None:
    a = none_assessment()
    assert a.state_at(T0 + MAX_EVIDENCE_AGE + timedelta(seconds=1)) is U  # type: ignore[attr-defined]


def test_evidence_from_the_future_is_not_trusted() -> None:
    a = none_assessment()
    assert a.state_at(T0 - timedelta(seconds=60)) is U  # type: ignore[attr-defined]


def test_old_active_still_blocks() -> None:
    a = check_rentals(
        fake_context(),
        sources=[Fixed("containers", A), Fixed("vms", N)],
        clock=lambda: T0,
    )
    assert a.state_at(T0 + timedelta(hours=1)) is A


def test_a_state_change_between_checks_is_seen_by_a_new_check() -> None:
    """The fix engine re-checks right before it changes anything (step 14)."""
    before = check_rentals(
        fake_context(),
        sources=[Fixed("containers", N), Fixed("vms", N)],
        clock=lambda: T0,
    )
    after = check_rentals(
        fake_context(),
        sources=[Fixed("containers", A), Fixed("vms", N)],
        clock=lambda: T0 + timedelta(seconds=5),
    )
    assert before.state is N and after.state is A


def test_assess_does_not_depend_on_the_order_of_readings() -> None:
    readings = [
        SourceReading("containers", N, "no containers", T0),
        SourceReading("vms", U, "not implemented", T0),
    ]
    forward = assess(readings, now=T0)
    backward = assess(list(reversed(readings)), now=T0)
    assert forward.state is backward.state is U
    assert forward.reason == backward.reason == "not implemented"
    assert forward.unresolved == backward.unresolved == ["vms"]


# -- the facts shown by `diagnose` ----------------------------------------------------


def facts_for(ctx: HostContext) -> dict[str, Fact]:
    return {f.key: f for f in RentalsCollector().collect(ctx)}


def test_collector_reports_unknown_as_a_value_with_the_reason() -> None:
    facts = facts_for(docker("running\nexited\n"))
    assert facts["rentals.state"].value == "active"
    assert facts["rentals.blocks_disruptive_fixes"].value is True
    assert facts["rentals.containers_total"].value == 2
    assert facts["rentals.containers_running"].value == 1
    assert facts["rentals.unresolved_sources"].value == "vms"


def test_collector_state_is_unknown_with_no_containers_because_the_vm_source_is_a_stub() -> (
    None
):
    facts = facts_for(docker(""))
    assert facts["rentals.state"].value == "unknown"
    assert facts["rentals.blocks_disruptive_fixes"].value is True
    assert "VM rental detection is not implemented" in str(
        facts["rentals.reason"].value
    )


def test_collector_when_docker_cannot_be_read() -> None:
    facts = facts_for(docker("", CommandStatus.MISSING))
    assert facts["rentals.state"].value == "unknown"
    assert "rentals.containers_total" not in facts  # no count from no data


def test_rental_facts_are_safe_to_share_and_carry_no_container_data() -> None:
    facts = facts_for(docker("running\n"))
    for fact in facts.values():
        fact.to_shareable()
    dumped = " ".join(f.model_dump_json() for f in facts.values())
    assert "source_command" in dumped  # internal facts keep the command ...
    assert "raw_excerpt" in dumped
    shareable = " ".join(f.to_shareable().model_dump_json() for f in facts.values())
    assert "docker" not in shareable  # ... shareable ones do not


# -- the operation reads state only, through the pinned endpoint ---------------------


def test_operation_asks_for_container_state_only() -> None:
    op = READ_OPERATIONS[OP]
    assert op.docker
    assert op.args == ("ps", "-a", "--format", "{{.State}}")
    assert not op.needs_root
    joined = " ".join(op.args)
    for forbidden in (
        ".Names",
        ".ID",
        ".Image",
        ".Env",
        ".Command",
        ".Labels",
        "logs",
        "inspect",
    ):
        assert forbidden not in joined


def test_operation_is_pinned_to_the_local_docker_socket() -> None:
    runner = SubprocessRunner(
        READ_OPERATIONS, environ={}, which=lambda n, p: f"/usr/bin/{n}"
    )
    argv, early = runner.plan(OP)
    assert early is None
    assert argv[:3] == ["/usr/bin/docker", "--host", TRUSTED_DOCKER_ENDPOINT]
    remote = SubprocessRunner(
        READ_OPERATIONS,
        environ={"DOCKER_HOST": "tcp://evil:2375"},
        which=lambda n, p: f"/usr/bin/{n}",
    )
    assert remote.plan(OP)[1].status is CommandStatus.REFUSED  # type: ignore[union-attr]


# -- wiring ----------------------------------------------------------------------------


def test_registry_includes_rentals() -> None:
    assert "rentals" in COLLECTORS


def test_diagnose_shows_the_rental_state_and_that_fixes_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    ctx = docker("")
    monkeypatch.setattr(cli, "default_context", lambda **kw: ctx)
    result = CliRunner().invoke(cli.app, ["diagnose"])
    assert "Rental state: unknown (disruptive fixes are blocked)" in result.stdout
