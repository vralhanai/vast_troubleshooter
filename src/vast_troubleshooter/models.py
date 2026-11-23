"""Core data models shared by collectors, rules, fixes, and reports.

Two layers on purpose:

* ``Fact`` is the internal record a collector produces. It may keep the source
  command and a short raw excerpt for local debugging.
* ``ShareableFact`` is the allowlisted, typed view that may appear in reports.
  It never carries raw log lines, command lines, or excerpts.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# Shareable string values are short and contain no control characters.
MAX_SHAREABLE_STRING = 200

Scalar = str | int | float | bool | None


class FactStatus(StrEnum):
    """Outcome of reading one fact."""

    OK = "ok"
    MISSING = "missing"  # the tool or file needed is not present
    ERROR = "error"  # the read failed
    TIMEOUT = "timeout"  # the read took too long


class CheckStatus(StrEnum):
    """Outcome of evaluating one check or rule. Unknown is never a pass."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Category(StrEnum):
    """Which Vast status a problem affects."""

    OFFLINE = "offline"
    UNVERIFIED = "unverified"
    PERFORMANCE = "performance"
    INFO = "info"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Name = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class ShareableFact(_Model):
    """A fact that is safe to put in a report. Allowlisted fields only."""

    collector: Name
    key: Name
    value: Scalar = None
    unit: str | None = Field(default=None, max_length=20)
    status: FactStatus
    timestamp: datetime

    @field_validator("value")
    @classmethod
    def _value_is_safe(cls, value: Scalar) -> Scalar:
        if isinstance(value, str):
            if len(value) > MAX_SHAREABLE_STRING:
                raise ValueError(f"string value longer than {MAX_SHAREABLE_STRING}")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError("string value contains control characters")
        return value


class Fact(_Model):
    """Internal record produced by a collector."""

    collector: Name
    key: Name
    value: Scalar = None
    unit: str | None = Field(default=None, max_length=20)
    status: FactStatus = FactStatus.OK
    source_command: str | None = None
    raw_excerpt: str | None = Field(default=None, max_length=2000)
    timestamp: datetime = Field(default_factory=_utcnow)

    def to_shareable(self) -> ShareableFact:
        """Drop source_command and raw_excerpt. Validates the value is safe."""
        return ShareableFact(
            collector=self.collector,
            key=self.key,
            value=self.value,
            unit=self.unit,
            status=self.status,
            timestamp=self.timestamp,
        )


class Finding(_Model):
    """A diagnosis produced by a rule."""

    rule_id: Name
    severity: Severity
    category: Category
    status: CheckStatus = CheckStatus.FAIL
    summary: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    evidence: list[Name] = Field(default_factory=list)  # fact keys, not raw text
    recommendation: Annotated[str, StringConstraints(max_length=1000)] = ""
    fix_id: Name | None = None
