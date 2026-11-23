import pytest
from typer.testing import CliRunner

from vast_troubleshooter import cli
from vast_troubleshooter.fixes import FixOutcome, FixStatus
from vast_troubleshooter.testing import fake_context

runner = CliRunner()
CMD = ("/usr/bin/systemctl", "restart", "vastai.service")


def outcome(status: FixStatus, **kw: object) -> FixOutcome:
    base: dict[str, object] = {
        "fix_id": "restart-vast-daemon",
        "run_id": "abc",
        "target": "the unit",
        "expected_change": "it runs",
        "undo_notes": "cannot be undone",
        "commands": (CMD,),
        "warnings": ("customers may be interrupted",),
        "rental_state": "none",
    }
    base.update(kw)
    return FixOutcome(status=status, **base)  # type: ignore[arg-type]


class StubEngine:
    """Answers a dry run, then an apply, and records how it was called."""

    def __init__(self, dry: FixOutcome, applied: FixOutcome | None = None) -> None:
        self.dry, self.applied = dry, applied
        self.calls: list[dict[str, object]] = []

    def run(self, fix_id: str, **kw: object) -> FixOutcome:
        self.calls.append({"fix_id": fix_id, **kw})
        return self.applied if kw.get("apply") and self.applied else self.dry


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    engine: StubEngine,
    *args: str,
    text: str | None = None,
):
    seen: dict[str, object] = {}

    def make_engine(ctx: object, use_sudo: bool = False) -> StubEngine:
        seen["use_sudo"] = use_sudo
        return engine

    monkeypatch.setattr(cli, "default_fix_engine", make_engine)
    monkeypatch.setattr(cli, "default_context", lambda **kw: fake_context())
    result = runner.invoke(cli.app, ["fix", *args], input=text)
    return result, seen


def test_with_no_argument_it_lists_the_fixes_and_marks_the_demo_one() -> None:
    result = runner.invoke(cli.app, ["fix"])
    assert result.exit_code == 0
    assert "restart-vast-daemon:" in result.stdout
    assert "fix-dns-resolver (demo only):" in result.stdout


def test_a_dry_run_is_the_default_shows_the_plan_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = StubEngine(outcome(FixStatus.DRY_RUN, before="fail"))
    result, _ = invoke(monkeypatch, engine, "restart-vast-daemon")
    assert result.exit_code == 0
    assert "DRY_RUN" in result.stdout
    assert "/usr/bin/systemctl restart vastai.service" in result.stdout
    assert "nothing has been changed" in result.stdout
    assert "warning: customers may be interrupted" in result.stdout
    assert "Dry run only. Add --apply" in result.stdout
    assert [c.get("apply", False) for c in engine.calls] == [False]


def test_apply_asks_for_the_fix_id_and_runs_only_when_it_is_typed_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = StubEngine(
        outcome(FixStatus.DRY_RUN),
        outcome(FixStatus.SUCCEEDED, before="fail", after="pass"),
    )
    result, _ = invoke(
        monkeypatch,
        engine,
        "restart-vast-daemon",
        "--apply",
        text="restart-vast-daemon\n",
    )
    assert result.exit_code == 0
    assert "Type the fix id (restart-vast-daemon) to confirm" in result.stdout
    assert "SUCCEEDED" in result.stdout
    assert engine.calls[-1]["apply"] is True
    assert engine.calls[-1]["confirmation"] == "restart-vast-daemon"
    assert "check before: fail, after: pass" in result.stdout


@pytest.mark.parametrize(
    "typed",
    ["yes\n", "y\n", "RESTART-VAST-DAEMON\n", " restart-vast-daemon\n", "\n", ""],
)
def test_anything_but_the_exact_id_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    engine = StubEngine(outcome(FixStatus.DRY_RUN), outcome(FixStatus.SUCCEEDED))
    result, _ = invoke(
        monkeypatch, engine, "restart-vast-daemon", "--apply", text=typed
    )
    assert result.exit_code == 1
    assert "Not confirmed. Nothing was changed." in result.stdout
    assert [c.get("apply", False) for c in engine.calls] == [False]  # never applied


def test_a_blocked_dry_run_does_not_ask_for_a_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = outcome(
        FixStatus.BLOCKED,
        reasons=("rental state is active: a container exists",),
        block_code="rental_active",
    )
    engine = StubEngine(blocked)
    result, _ = invoke(
        monkeypatch,
        engine,
        "restart-vast-daemon",
        "--apply",
        text="restart-vast-daemon\n",
    )
    assert result.exit_code == 1
    assert "BLOCKED" in result.stdout and "rental state is active" in result.stdout
    assert "Type the fix id" not in result.stdout
    assert len(engine.calls) == 1


def test_a_confirmed_apply_that_is_then_blocked_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = StubEngine(
        outcome(FixStatus.DRY_RUN),
        outcome(
            FixStatus.BLOCKED,
            reasons=("rental state is now active: x",),
            block_code="state_changed",
        ),
    )
    result, _ = invoke(
        monkeypatch,
        engine,
        "restart-vast-daemon",
        "--apply",
        text="restart-vast-daemon\n",
    )
    assert result.exit_code == 1
    assert "rental state is now active" in result.stdout


@pytest.mark.parametrize(
    ("status", "code"),
    [(FixStatus.SUCCEEDED, 0), (FixStatus.FAILED, 4), (FixStatus.INDETERMINATE, 5)],
)
def test_exit_codes_for_a_run_that_changed_the_system(
    monkeypatch: pytest.MonkeyPatch, status: FixStatus, code: int
) -> None:
    engine = StubEngine(outcome(FixStatus.DRY_RUN), outcome(status, reasons=("why",)))
    result, _ = invoke(
        monkeypatch,
        engine,
        "restart-vast-daemon",
        "--apply",
        text="restart-vast-daemon\n",
    )
    assert result.exit_code == code


def test_a_rolled_back_failure_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = StubEngine(
        outcome(FixStatus.DRY_RUN),
        outcome(
            FixStatus.FAILED, rolled_back=True, reasons=("still present", "rolled back")
        ),
    )
    result, _ = invoke(
        monkeypatch, engine, "fix-dns-resolver", "--apply", text="fix-dns-resolver\n"
    )
    assert "the change was rolled back" in result.stdout


def test_the_flags_are_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = StubEngine(outcome(FixStatus.DRY_RUN))
    _, seen = invoke(
        monkeypatch, engine, "fix-dns-resolver", "--use-sudo", "--demo-disposable-host"
    )
    assert seen["use_sudo"] is True
    assert engine.calls[0]["allow_demo"] is True


def test_the_defaults_are_no_sudo_and_no_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = StubEngine(outcome(FixStatus.DRY_RUN))
    _, seen = invoke(monkeypatch, engine, "restart-vast-daemon")
    assert seen["use_sudo"] is False
    assert engine.calls[0]["allow_demo"] is False


@pytest.mark.parametrize(
    "shortcut", ["--yes", "-y", "--force", "--no-confirm", "--confirm"]
)
def test_there_is_no_way_to_skip_the_typed_confirmation(shortcut: str) -> None:
    result = runner.invoke(cli.app, ["fix", "restart-vast-daemon", "--apply", shortcut])
    assert result.exit_code == 2  # unknown option


def test_a_real_run_with_an_unknown_fix_is_blocked() -> None:
    result = runner.invoke(cli.app, ["fix", "restart-everything"])
    assert result.exit_code == 1
    assert "BLOCKED" in result.stdout and "not a known fix" in result.stdout


def test_host_text_in_the_output_cannot_drive_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nasty = outcome(
        FixStatus.BLOCKED, reasons=("bad\x1b[31m[red]x[/red]\nline",), block_code="x"
    )
    result, _ = invoke(monkeypatch, StubEngine(nasty), "restart-vast-daemon")
    assert "\x1b" not in result.stdout
    assert "[red]x[/red]" in result.stdout  # printed literally, not as markup


def test_help_lists_the_fix_command() -> None:
    assert "fix" in runner.invoke(cli.app, ["--help"]).stdout
