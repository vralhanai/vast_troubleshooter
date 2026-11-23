"""Rules as data: a closed schema, a strict loader, and a three-valued evaluator.

A rule file can only say *what to check* and *what to tell the operator*. It
cannot run code, name a command, or approve a fix: `fix_id` is only a name that
reviewed code (the fix registry, step 14) may or may not know.

Evaluation never turns missing data into a pass. Every check is TRUE, FALSE, or
UNKNOWN, and a rule whose inputs are unknown reports UNKNOWN, not PASS.
"""

import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)

from vast_troubleshooter.models import (
    Category,
    CheckStatus,
    Fact,
    FactStatus,
    Finding,
    Severity,
)

MAX_FILE_BYTES = 256 * 1024
MAX_RULES = 500
MAX_DEPTH = 4
MAX_NODES = 50
SCHEMA_VERSION = 1

# Anchored: pydantic patterns are searches, so without ^ and $ "Bad Id!" would pass.
_ID = r"^[a-z0-9][a-z0-9_.-]{0,99}$"
_FACT = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
Scalar = bool | StrictInt | StrictFloat | str
Number = StrictInt | StrictFloat


class RuleFileError(ValueError):
    """A rule file is malformed or unsafe. Nothing from it is loaded."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- conditions: a small, fixed set. No expressions, no code. -------------------


class _Compare(_Closed):
    fact: str = Field(pattern=_FACT)
    other_fact: str | None = Field(default=None, pattern=_FACT)

    @model_validator(mode="after")
    def _one_operand(self) -> _Compare:
        has_value = getattr(self, "value", None) is not None
        if has_value == (self.other_fact is not None):
            raise ValueError("give exactly one of 'value' or 'other_fact'")
        return self


class Equals(_Compare):
    type: Literal["equals"]
    value: Scalar | None = None


class NotEquals(_Compare):
    type: Literal["not_equals"]
    value: Scalar | None = None


class LessThan(_Compare):
    type: Literal["less_than"]
    value: Number | None = None


class GreaterThan(_Compare):
    type: Literal["greater_than"]
    value: Number | None = None


class Contains(_Closed):
    """True if `value` is one of the comma-separated tokens of the fact."""

    type: Literal["contains"]
    fact: str = Field(pattern=_FACT)
    value: str = Field(min_length=1, max_length=100)


class Missing(_Closed):
    """True if the fact is absent or its tool is not installed."""

    type: Literal["missing"]
    fact: str = Field(pattern=_FACT)


class All(_Closed):
    type: Literal["all"]
    of: list[Condition] = Field(min_length=1, max_length=20)


class Any(_Closed):
    type: Literal["any"]
    of: list[Condition] = Field(min_length=1, max_length=20)


Condition = Annotated[
    Equals | NotEquals | LessThan | GreaterThan | Contains | Missing | All | Any,
    Field(discriminator="type"),
]
All.model_rebuild()
Any.model_rebuild()


def _walk(cond: Condition, depth: int = 1) -> Iterable[tuple[Condition, int]]:
    yield cond, depth
    if isinstance(cond, All | Any):
        for child in cond.of:
            yield from _walk(child, depth + 1)


def fact_keys(cond: Condition) -> list[str]:
    keys: set[str] = set()
    for node, _ in _walk(cond):
        if isinstance(node, All | Any):
            continue
        keys.add(node.fact)
        other = getattr(node, "other_fact", None)
        if other:
            keys.add(other)
    return sorted(keys)


class Rule(_Closed):
    id: str = Field(pattern=_ID)
    title: str = Field(min_length=1, max_length=200)
    severity: Severity
    category: Category
    recommendation: str = Field(default="", max_length=1000)
    # Only a name. Whether it exists is decided by reviewed code (step 14).
    fix_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    # Where the threshold comes from, and when it was last checked.
    source: str = Field(min_length=1, max_length=300)
    checked: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    only_if: Condition | None = None
    when: Condition

    @model_validator(mode="after")
    def _bounded(self) -> Rule:
        for cond in (self.when, self.only_if):
            if cond is None:
                continue
            nodes = list(_walk(cond))
            if len(nodes) > MAX_NODES:
                raise ValueError(f"more than {MAX_NODES} conditions")
            if max(depth for _, depth in nodes) > MAX_DEPTH:
                raise ValueError(f"conditions nested deeper than {MAX_DEPTH}")
        return self


class _RuleFile(_Closed):
    schema_version: int
    rule: list[Rule] = Field(min_length=1)


# -- loading ---------------------------------------------------------------------


def load_rules_text(
    text: str, name: str = "<text>", known_fix_ids: Iterable[str] | None = None
) -> list[Rule]:
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise RuleFileError(f"{name}: larger than {MAX_FILE_BYTES} bytes")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuleFileError(f"{name}: invalid TOML: {exc}") from exc
    try:
        parsed = _RuleFile.model_validate(data)
    except ValidationError as exc:
        raise RuleFileError(f"{name}: {_summarize(exc)}") from exc
    if parsed.schema_version != SCHEMA_VERSION:
        raise RuleFileError(
            f"{name}: schema_version {parsed.schema_version} is not supported "
            f"(expected {SCHEMA_VERSION})"
        )
    if known_fix_ids is not None:
        known = set(known_fix_ids)
        for rule in parsed.rule:
            if rule.fix_id and rule.fix_id not in known:
                raise RuleFileError(
                    f"{name}: rule {rule.id} names unknown fix {rule.fix_id!r}"
                )
    return list(parsed.rule)


def _summarize(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    return f"{first['msg']} (at {where})"


def load_rules_dir(
    directory: Path | resources.abc.Traversable,
    known_fix_ids: Iterable[str] | None = None,
) -> list[Rule]:
    """Load every *.toml in a directory, in file-name order. All or nothing."""
    files = sorted(
        (f for f in directory.iterdir() if f.name.endswith(".toml")),
        key=lambda f: f.name,
    )
    rules: list[Rule] = []
    seen: dict[str, str] = {}
    for file in files:
        for rule in load_rules_text(
            file.read_text(encoding="utf-8"), file.name, known_fix_ids
        ):
            if rule.id in seen:
                raise RuleFileError(
                    f"{file.name}: duplicate rule id {rule.id!r} (also in {seen[rule.id]})"
                )
            seen[rule.id] = file.name
            rules.append(rule)
    if len(rules) > MAX_RULES:
        raise RuleFileError(f"more than {MAX_RULES} rules")
    return rules


def load_default_rules(known_fix_ids: Iterable[str] | None = None) -> list[Rule]:
    return load_rules_dir(
        resources.files("vast_troubleshooter") / "rules", known_fix_ids
    )


# -- evaluation ------------------------------------------------------------------


class Tri(Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    status: CheckStatus
    severity: Severity
    category: Category
    title: str = ""
    finding: Finding | None = None
    # For UNKNOWN: which inputs were not usable, e.g. "kernel_log.xid_count: needs_root".
    reasons: tuple[str, ...] = ()


Facts = dict[str, Fact]


def facts_by_key(facts: Iterable[Fact]) -> Facts:
    return {f.key: f for f in facts}


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _fetch(facts: Facts, key: str) -> tuple[object, str | None]:
    fact = facts.get(key)
    if fact is None:
        return None, f"{key}: no data"
    if fact.status is not FactStatus.OK:
        return None, f"{key}: {fact.status.value}"
    if fact.value is None:
        return None, f"{key}: no value"
    return fact.value, None


def _operands(cond: Equals | NotEquals | LessThan | GreaterThan, facts: Facts):
    left, why = _fetch(facts, cond.fact)
    reasons = [why] if why else []
    if cond.other_fact is not None:
        right, why = _fetch(facts, cond.other_fact)
        if why:
            reasons.append(why)
    else:
        right = cond.value
    return left, right, reasons


def _eval(cond: Condition, facts: Facts) -> tuple[Tri, list[str]]:
    if isinstance(cond, All | Any):
        results = [_eval(child, facts) for child in cond.of]
        reasons = [r for _, rs in results for r in rs]
        states = [t for t, _ in results]
        decisive = Tri.FALSE if isinstance(cond, All) else Tri.TRUE
        if decisive in states:
            return decisive, []
        if Tri.UNKNOWN in states:
            return Tri.UNKNOWN, reasons
        return (Tri.TRUE if isinstance(cond, All) else Tri.FALSE), []

    if isinstance(cond, Missing):
        fact = facts.get(cond.fact)
        if fact is None or fact.status is FactStatus.MISSING:
            return Tri.TRUE, []
        if fact.status is FactStatus.OK:
            return Tri.FALSE, []
        return Tri.UNKNOWN, [f"{cond.fact}: {fact.status.value}"]

    if isinstance(cond, Contains):
        value, why = _fetch(facts, cond.fact)
        if why:
            return Tri.UNKNOWN, [why]
        if not isinstance(value, str):
            return Tri.UNKNOWN, [f"{cond.fact}: not a text value"]
        tokens = [t.strip() for t in value.split(",")]
        return (Tri.TRUE if cond.value in tokens else Tri.FALSE), []

    left, right, reasons = _operands(cond, facts)
    if reasons:
        return Tri.UNKNOWN, reasons
    if isinstance(cond, Equals | NotEquals):
        if isinstance(left, bool) != isinstance(right, bool) or (
            isinstance(left, str) != isinstance(right, str)
        ):
            return Tri.UNKNOWN, [f"{cond.fact}: type mismatch"]
        same = left == right
        return (Tri.TRUE if same == isinstance(cond, Equals) else Tri.FALSE), []
    if not (_number(left) and _number(right)):
        return Tri.UNKNOWN, [f"{cond.fact}: not comparable as numbers"]
    holds = left < right if isinstance(cond, LessThan) else left > right  # type: ignore[operator]
    return (Tri.TRUE if holds else Tri.FALSE), []


def evaluate_rule(rule: Rule, facts: Facts) -> RuleResult:
    def result(status: CheckStatus, **extra: object) -> RuleResult:
        return RuleResult(
            rule.id, status, rule.severity, rule.category, rule.title, **extra
        )  # type: ignore[arg-type]

    if rule.only_if is not None:
        applies, reasons = _eval(rule.only_if, facts)
        if applies is Tri.FALSE:
            return result(CheckStatus.SKIPPED)
        if applies is Tri.UNKNOWN:
            return result(CheckStatus.UNKNOWN, reasons=tuple(reasons))

    outcome, reasons = _eval(rule.when, facts)
    if outcome is Tri.TRUE:
        finding = Finding(
            rule_id=rule.id,
            severity=rule.severity,
            category=rule.category,
            status=CheckStatus.FAIL,
            summary=rule.title,
            evidence=fact_keys(rule.when),
            recommendation=rule.recommendation,
            fix_id=rule.fix_id,
        )
        return result(CheckStatus.FAIL, finding=finding)
    if outcome is Tri.FALSE:
        return result(CheckStatus.PASS)
    return result(CheckStatus.UNKNOWN, reasons=tuple(reasons))


def evaluate_rules(rules: Sequence[Rule], facts: Iterable[Fact]) -> list[RuleResult]:
    """One result per rule, in rule order. Same facts always give the same results."""
    by_key = facts_by_key(facts)
    return [evaluate_rule(rule, by_key) for rule in rules]


_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def findings_of(results: Iterable[RuleResult]) -> list[Finding]:
    """Findings, most severe first, then by rule id."""
    found = [r.finding for r in results if r.finding is not None]
    return sorted(found, key=lambda f: (_SEVERITY_ORDER[f.severity], f.rule_id))
