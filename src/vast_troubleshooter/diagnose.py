"""Run every collector, evaluate every rule, and decide the exit code.

Exit codes:
  0  complete, no critical findings
  1  at least one critical finding
  2  the tool itself failed (not produced here; the CLI uses it for errors)
  3  incomplete: no critical finding, but a critical check could not be decided

A check that is *skipped* (it does not apply, for example the outbound-probe
rule when probes were not run) is not incomplete. Only *unknown* is.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from vast_troubleshooter.collectors import get_collectors, run_collectors
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import CheckStatus, Fact, Finding, Severity
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.rule_engine import (
    Rule,
    RuleResult,
    evaluate_rules,
    findings_of,
)

EXIT_OK = 0
EXIT_CRITICAL = 1
EXIT_TOOL_ERROR = 2
EXIT_INCOMPLETE = 3

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class Diagnosis:
    facts: tuple[Fact, ...]
    results: tuple[RuleResult, ...]

    @property
    def findings(self) -> list[Finding]:
        return findings_of(self.results)

    @property
    def unknown(self) -> list[RuleResult]:
        found = [r for r in self.results if r.status is CheckStatus.UNKNOWN]
        return sorted(found, key=lambda r: (_SEVERITY_ORDER[r.severity], r.rule_id))

    @property
    def incomplete(self) -> bool:
        return any(r.severity is Severity.CRITICAL for r in self.unknown)

    @property
    def needs_root_hint(self) -> bool:
        return any("needs_root" in reason for r in self.unknown for reason in r.reasons)

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in CheckStatus}
        for result in self.results:
            counts[result.status.value] += 1
        return counts

    @property
    def exit_code(self) -> int:
        if any(f.severity is Severity.CRITICAL for f in self.findings):
            return EXIT_CRITICAL
        return EXIT_INCOMPLETE if self.incomplete else EXIT_OK


def run_diagnosis(ctx: HostContext, rules: Sequence[Rule]) -> Diagnosis:
    facts = run_collectors(get_collectors(), ctx)
    return Diagnosis(tuple(facts), tuple(evaluate_rules(rules, facts)))


def elevated_commands() -> list[str]:
    """The fixed commands that need root, as they would run with --allow-root-reads."""
    lines = []
    for op in READ_OPERATIONS.values():
        if op.needs_root:
            lines.append(" ".join([op.executable, *op.args]))
    return sorted(lines)
