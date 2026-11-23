"""Command-line interface for vast-troubleshooter."""

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from vast_troubleshooter import __version__
from vast_troubleshooter.collectors import (
    UnknownCollectorError,
    get_collectors,
    run_collectors,
)
from vast_troubleshooter.context import default_context
from vast_troubleshooter.diagnose import (
    EXIT_TOOL_ERROR,
    Diagnosis,
    elevated_commands,
    run_diagnosis,
)
from vast_troubleshooter.fixes import FIXES, FixOutcome, FixStatus, default_fix_engine
from vast_troubleshooter.fixes.storage import state_dir
from vast_troubleshooter.models import Fact, Severity
from vast_troubleshooter.redact import default_redactor
from vast_troubleshooter.report import (
    ReportError,
    make_report_files,
    read_actions,
    write_report,
)
from vast_troubleshooter.rule_engine import RuleFileError, load_default_rules

app = typer.Typer(
    name="vast-troubleshooter",
    help="Diagnose and fix issues on Vast.ai host machines.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Diagnose and fix issues on Vast.ai host machines."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def safe_text(text: str) -> str:
    """Replace control characters so host data cannot drive the terminal."""
    return "".join("?" if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)


def format_fact(fact: Fact) -> str:
    value = "" if fact.value is None else str(fact.value)
    unit = f" {fact.unit}" if fact.unit and value else ""
    detail = f"  ({fact.raw_excerpt})" if fact.raw_excerpt else ""
    return safe_text(f"{fact.key} = {value}{unit} [{fact.status.value}]{detail}")


@app.command()
def collect(
    name: str = typer.Argument(..., help="Collector to run, for example: system"),
    allow_outbound: bool = typer.Option(
        False,
        "--allow-outbound",
        help="Allow probes that connect to third-party servers. Off by default.",
    ),
) -> None:
    """Run one collector and print the facts it read. Read-only."""
    try:
        collectors = get_collectors([name])
    except UnknownCollectorError as exc:
        typer.echo(f"error: {safe_text(str(exc))}", err=True)
        raise typer.Exit(2) from exc
    if allow_outbound:
        for collector in collectors:
            for line in collector.outbound_notice():
                typer.echo(safe_text(line), err=True)
    for fact in run_collectors(collectors, default_context(allow_outbound)):
        typer.echo(format_fact(fact))


_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


def evidence_line(key: str, facts: dict[str, Fact]) -> str:
    fact = facts.get(key)
    if fact is None:
        return f"{key}: not collected"
    value = (
        ""
        if fact.value is None
        else f" = {fact.value}{' ' + fact.unit if fact.unit else ''}"
    )
    return f"{key}{value} [{fact.status.value}]"


def render_human(diagnosis: Diagnosis, console: Console) -> None:
    def out(text: str = "") -> None:
        console.print(text, soft_wrap=True, highlight=False)

    facts = {f.key: f for f in diagnosis.facts}
    counts = diagnosis.counts()
    out("[bold]Vast Troubleshooter diagnosis[/bold]")
    if diagnosis.environment:
        out(
            f"[bold yellow]Environment: {escape(safe_text(diagnosis.environment))}.[/bold yellow] "
            "These results describe this environment, not a Vast host. The kernel log, drives, "
            "systemd, GPUs, and rentals seen here are not representative."
        )
    out(
        f"{len(diagnosis.results)} checks: {counts['fail']} problem(s), "
        f"{counts['pass']} ok, {counts['unknown']} could not be checked, "
        f"{counts['skipped']} not applicable."
    )
    rental = facts.get("rentals.state")
    if rental is not None and rental.value is not None:
        out(
            f"Rental state: {rental.value}"
            + (" (disruptive fixes are blocked)" if rental.value != "none" else "")
        )
    findings = diagnosis.findings
    out()
    if not findings:
        note = (
            " in the checks that ran"
            if diagnosis.incomplete or counts["unknown"]
            else ""
        )
        out(f"[green]No problems found{note}.[/green]")
    for finding in findings:
        style = _STYLE[finding.severity]
        out(
            f"[{style}]{finding.severity.value.upper()}[/{style}] "
            f"{escape(safe_text(finding.summary))} [dim]({finding.category.value})[/dim]"
        )
        for key in finding.evidence:
            out(f"  evidence: {escape(safe_text(evidence_line(key, facts)))}")
        if finding.recommendation:
            out(f"  what to do: {escape(safe_text(finding.recommendation))}")
        out()
    unknown = diagnosis.unknown
    if unknown:
        out(f"[bold]Could not be checked ({len(unknown)}):[/bold]")
        for result in unknown:
            title = result.title or result.rule_id
            reasons = ", ".join(result.reasons) or "no data"
            out(
                f"  {escape(safe_text(title))} [dim]({result.severity.value})[/dim]: {escape(safe_text(reasons))}"
            )
        if diagnosis.needs_root_hint:
            out(
                "  Some checks need root. Re-run with sudo, or with --allow-root-reads."
            )
        if diagnosis.incomplete:
            out(
                "[yellow]The diagnosis is incomplete: a critical check could not be decided.[/yellow]"
            )


def to_json(diagnosis: Diagnosis) -> dict[str, object]:
    shareable = []
    omitted = 0
    for fact in diagnosis.facts:
        try:
            shareable.append(json.loads(fact.to_shareable().model_dump_json()))
        except ValueError:
            omitted += 1
    return {
        "schema_version": 1,
        "exit_code": diagnosis.exit_code,
        "complete": not diagnosis.incomplete,
        "counts": diagnosis.counts(),
        "findings": [json.loads(f.model_dump_json()) for f in diagnosis.findings],
        "unknown": [
            {
                "rule_id": r.rule_id,
                "severity": r.severity.value,
                "reasons": list(r.reasons),
            }
            for r in diagnosis.unknown
        ],
        "facts": shareable,
        "omitted_facts": omitted,
    }


def _collect_diagnosis(allow_root_reads: bool, allow_outbound: bool) -> Diagnosis:
    """Load the rules, announce any opt-in, and run the diagnosis. Exits 2 on a tool error."""
    try:
        rules = load_default_rules()
    except RuleFileError as exc:
        typer.echo(f"error: {safe_text(str(exc))}", err=True)
        raise typer.Exit(EXIT_TOOL_ERROR) from exc

    if allow_root_reads:
        typer.echo(
            "Elevated reads are enabled. These read-only commands may run with 'sudo -n':",
            err=True,
        )
        for command in elevated_commands():
            typer.echo(f"  {safe_text(command)}", err=True)
    if allow_outbound:
        for collector in get_collectors():
            for line in collector.outbound_notice():
                typer.echo(safe_text(line), err=True)

    ctx = default_context(
        allow_outbound=allow_outbound, allow_elevated=allow_root_reads
    )
    try:
        return run_diagnosis(ctx, rules)
    except Exception as exc:
        typer.echo(
            f"error: diagnosis failed: {safe_text(type(exc).__name__)}: {safe_text(str(exc))}",
            err=True,
        )
        raise typer.Exit(EXIT_TOOL_ERROR) from exc


@app.command()
def diagnose(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    allow_root_reads: bool = typer.Option(
        False,
        "--allow-root-reads",
        help="Let read-only checks that need root run through a fixed 'sudo -n'.",
    ),
    allow_outbound: bool = typer.Option(
        False,
        "--allow-outbound",
        help="Allow probes that connect to third-party servers. Off by default.",
    ),
) -> None:
    """Check this host and report problems. Read-only: nothing is changed."""
    diagnosis = _collect_diagnosis(allow_root_reads, allow_outbound)

    if as_json:
        typer.echo(json.dumps(to_json(diagnosis), indent=2))
    else:
        render_human(diagnosis, Console())
    raise typer.Exit(diagnosis.exit_code)


# Exit codes for `fix`: 0 dry run or success, 1 blocked, 2 tool error,
# 4 the fix ran and failed, 5 it ran and the result cannot be told.
_FIX_EXIT = {
    FixStatus.DRY_RUN: 0,
    FixStatus.SUCCEEDED: 0,
    FixStatus.BLOCKED: 1,
    FixStatus.FAILED: 4,
    FixStatus.INDETERMINATE: 5,
}


def render_fix(outcome: FixOutcome, console: Console) -> None:
    def out(text: str = "") -> None:
        console.print(text, soft_wrap=True, highlight=False)

    def clean(text: str) -> str:
        return escape(safe_text(text))

    colour = {
        FixStatus.SUCCEEDED: "green",
        FixStatus.DRY_RUN: "cyan",
        FixStatus.BLOCKED: "yellow",
        FixStatus.FAILED: "bold red",
        FixStatus.INDETERMINATE: "bold yellow",
    }[outcome.status]
    out(f"[{colour}]{outcome.status.value.upper()}[/{colour}] {clean(outcome.fix_id)}")
    for reason in outcome.reasons:
        out(f"  {clean(reason)}")
    if outcome.target:
        out(f"  target: {clean(outcome.target)}")
        out(f"  expected change: {clean(outcome.expected_change)}")
    for warning in outcome.warnings:
        out(f"  [yellow]warning:[/yellow] {clean(warning)}")
    if outcome.status is FixStatus.DRY_RUN:
        out("  would run (nothing has been changed):")
        for command in outcome.commands:
            out(f"    {clean(' '.join(command))}")
    if outcome.rental_state:
        out(f"  rental state: {clean(outcome.rental_state)}")
    if outcome.before or outcome.after:
        out(f"  check before: {outcome.before or '-'}, after: {outcome.after or '-'}")
    if outcome.rolled_back:
        out("  the change was rolled back")
    if outcome.rollback_failed:
        out(
            "  [bold red]ROLLBACK FAILED: the change may still be in place. "
            "Check the system by hand.[/bold red]"
        )
    if outcome.undo_notes and outcome.status is not FixStatus.BLOCKED:
        out(f"  how to undo: {clean(outcome.undo_notes)}")


@app.command()
def fix(
    fix_id: str = typer.Argument(
        None, help="The fix to run. Leave out to list the fixes."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Make the change. Without this, only show what would happen.",
    ),
    use_sudo: bool = typer.Option(
        False,
        "--use-sudo",
        help="Let the fixed commands that need root run through 'sudo -n'.",
    ),
    demo_disposable_host: bool = typer.Option(
        False,
        "--demo-disposable-host",
        help="Allow a demo-only fix. Only on a disposable test machine, never a live host.",
    ),
) -> None:
    """Show, or apply, a fix. A dry run is the default and changes nothing."""
    console = Console()
    if fix_id is None:
        for known in FIXES.values():
            tag = " (demo only)" if known.demo_only else ""
            typer.echo(safe_text(f"{known.id}{tag}: {known.description}"))
        raise typer.Exit(0)

    ctx = default_context(allow_elevated=use_sudo)
    try:
        engine = default_fix_engine(ctx, use_sudo=use_sudo)
        outcome = engine.run(fix_id, allow_demo=demo_disposable_host)
        render_fix(outcome, console)
        if outcome.status is FixStatus.DRY_RUN and apply:
            console.print()
            try:
                typed = typer.prompt(
                    f"Type the fix id ({fix_id}) to confirm, or anything else to cancel",
                    default="",
                    show_default=False,
                )
            except typer.Abort:
                typed = ""
            if typed != fix_id:
                typer.echo("Not confirmed. Nothing was changed.")
                raise typer.Exit(1)
            outcome = engine.run(
                fix_id, apply=True, confirmation=typed, allow_demo=demo_disposable_host
            )
            console.print()
            render_fix(outcome, console)
        elif outcome.status is FixStatus.DRY_RUN:
            typer.echo("Dry run only. Add --apply to make the change.")
    except (RuleFileError, ValueError) as exc:
        typer.echo(f"error: {safe_text(str(exc))}", err=True)
        raise typer.Exit(EXIT_TOOL_ERROR) from exc
    raise typer.Exit(_FIX_EXIT[outcome.status])


@app.command()
def report(
    output_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--output-dir",
        help="Where to write the report files. Default: a private folder in this tool's state directory.",
    ),
    show: bool = typer.Option(
        False, "--show", help="Also print the Markdown report here."
    ),
    allow_root_reads: bool = typer.Option(
        False,
        "--allow-root-reads",
        help="Let read-only checks that need root run through a fixed 'sudo -n'.",
    ),
    allow_outbound: bool = typer.Option(
        False,
        "--allow-outbound",
        help="Allow probes that connect to third-party servers. Off by default.",
    ),
) -> None:
    """Run the diagnosis and write a redacted report (JSON and Markdown) for Vast Support.

    Nothing is uploaded. Identifying text is always removed; there is no option to
    keep it. Read the files before you attach them.
    """
    diagnosis = _collect_diagnosis(allow_root_reads, allow_outbound)
    directory = output_dir or state_dir() / "reports"
    try:
        actions = read_actions(state_dir() / "actions.jsonl")
        json_text, markdown = make_report_files(diagnosis, default_redactor(), actions)
        json_path, md_path = write_report(directory, json_text, markdown)
    except ReportError as exc:
        typer.echo(f"error: {safe_text(str(exc))}", err=True)
        raise typer.Exit(EXIT_TOOL_ERROR) from exc

    typer.echo(
        "Report written. Nothing was uploaded. Read both files before you share them:"
    )
    for path in (json_path, md_path):
        typer.echo(f"  {safe_text(str(path))}  ({path.stat().st_size} bytes)")
    counts = diagnosis.counts()
    typer.echo(
        f"Diagnosis: {counts['fail']} problem(s), {counts['unknown']} could not be checked"
        f"{' (incomplete)' if diagnosis.incomplete else ''}."
    )
    if show:
        typer.echo("")
        typer.echo(markdown)
