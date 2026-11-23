"""Command-line interface for vast-troubleshooter."""

import typer

from vast_troubleshooter import __version__
from vast_troubleshooter.collectors import (
    UnknownCollectorError,
    get_collectors,
    run_collectors,
)
from vast_troubleshooter.context import default_context
from vast_troubleshooter.models import Fact

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
) -> None:
    """Run one collector and print the facts it read. Read-only."""
    try:
        collectors = get_collectors([name])
    except UnknownCollectorError as exc:
        typer.echo(f"error: {safe_text(str(exc))}", err=True)
        raise typer.Exit(2) from exc
    for fact in run_collectors(collectors, default_context()):
        typer.echo(format_fact(fact))
