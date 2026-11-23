"""Command-line interface for vast-troubleshooter."""

import typer

from vast_troubleshooter import __version__

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
