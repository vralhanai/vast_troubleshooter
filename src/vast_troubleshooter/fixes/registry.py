"""The fix registry and the mutation allowlist.

The mutation allowlist is separate from the read allowlist in `operations.py`:
the read runner can never run anything registered here, and the mutation
runner can never run anything registered there.
"""

from collections.abc import Mapping

from vast_troubleshooter.context import HostContext
from vast_troubleshooter.fixes.base import Fix
from vast_troubleshooter.fixes.definitions import (
    FIX_DNS_RESOLVER,
    MUTATION_OPS,
    RESTART_VAST_DAEMON,
)
from vast_troubleshooter.fixes.engine import FixEngine
from vast_troubleshooter.rule_engine import load_default_rules
from vast_troubleshooter.runner import Operation, SubprocessRunner

FIXES: Mapping[str, Fix] = {
    fix.id: fix for fix in (RESTART_VAST_DAEMON, FIX_DNS_RESOLVER)
}
MUTATION_OPERATIONS: Mapping[str, Operation] = {op.id: op for op in MUTATION_OPS}


def default_fix_engine(ctx: HostContext, use_sudo: bool = False) -> FixEngine:
    """The real engine. Every rule's `fix_id` must name a registered fix."""
    rules = load_default_rules(known_fix_ids=FIXES)
    runner = SubprocessRunner(MUTATION_OPERATIONS, allow_elevated=use_sudo)
    return FixEngine(FIXES, runner, ctx, rules)
