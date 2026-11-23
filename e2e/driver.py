"""TEST HARNESS ONLY. Never part of the tool.

Runs the REAL fix engine (real commands, real mutation allowlist, real rules) but
with an INJECTED VM-rental evidence source.

Why: in production the VM source is a stub that always says "unknown", so the CLI
refuses every disruptive fix. That is deliberate (see docs/rental-detection.md).
To prove the rest of the engine works end to end on a real systemd machine, this
script supplies a stand-in VM source that says "none" (or "unknown"). The
container evidence is NOT faked here: it comes from the real container source,
which asks the stand-in `docker` CLI on this test machine.

    driver.py <fix-id> [--apply] [--vm none|unknown]

It prints the same outcome text as `vast-troubleshooter fix`. With --apply it
supplies the confirmation (the fix id) itself; the interactive prompt is covered
by the CLI tests.
"""

import sys

from rich.console import Console

from vast_troubleshooter.cli import _FIX_EXIT, render_fix
from vast_troubleshooter.context import default_context
from vast_troubleshooter.fixes.engine import FixEngine
from vast_troubleshooter.fixes.registry import FIXES, MUTATION_OPERATIONS
from vast_troubleshooter.rentals import (
    ContainersSource,
    RentalState,
    SourceResult,
    check_rentals,
)
from vast_troubleshooter.rule_engine import load_default_rules
from vast_troubleshooter.runner import SubprocessRunner


class InjectedVms:
    """A stand-in VM source. The real one is a stub that always says unknown."""

    name = "vms"

    def __init__(self, state: RentalState) -> None:
        self._state = state

    def read(self, ctx):
        return SourceResult(
            self._state, f"TEST HARNESS: injected VM evidence '{self._state.value}'"
        )


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    fix_id = argv[0]
    apply = "--apply" in argv
    vm = (
        RentalState.UNKNOWN
        if "--vm" in argv and argv[argv.index("--vm") + 1] == "unknown"
        else RentalState.NONE
    )

    ctx = default_context()  # the real read-only context on this machine
    engine = FixEngine(
        FIXES,
        SubprocessRunner(
            MUTATION_OPERATIONS
        ),  # the real mutation allowlist, we are root
        ctx,
        load_default_rules(known_fix_ids=FIXES),
        rental_check=lambda c: check_rentals(
            c, sources=[ContainersSource(), InjectedVms(vm)]
        ),
    )
    console = Console(highlight=False, soft_wrap=True)
    console.print(
        f"[bold]TEST HARNESS[/bold]: injected VM evidence = {vm.value}; apply = {apply}"
    )
    outcome = engine.run(
        fix_id,
        apply=apply,
        confirmation=fix_id if apply else None,
        allow_demo=True,
    )
    render_fix(outcome, console)
    return _FIX_EXIT[outcome.status]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
