"""Command-line interface for vast-troubleshooter."""

import json

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
from vast_troubleshooter.models import Fact, Severity
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
    out(
        f"{len(diagnosis.results)} checks: {counts['fail']} problem(s), "
        f"{counts['pass']} ok, {counts['unknown']} could not be checked, "
        f"{counts['skipped']} not applicable."
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
        diagnosis = run_diagnosis(ctx, rules)
    except Exception as exc:
        typer.echo(
            f"error: diagnosis failed: {safe_text(type(exc).__name__)}: {safe_text(str(exc))}",
            err=True,
        )
        raise typer.Exit(EXIT_TOOL_ERROR) from exc

    if as_json:
        typer.echo(json.dumps(to_json(diagnosis), indent=2))
    else:
        render_human(diagnosis, Console())
    raise typer.Exit(diagnosis.exit_code)
