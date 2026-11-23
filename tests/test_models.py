import pytest
from pydantic import ValidationError

from vast_troubleshooter.models import (
    Category,
    CheckStatus,
    Fact,
    FactStatus,
    Finding,
    Severity,
    ShareableFact,
)


def make_fact(**overrides: object) -> Fact:
    data: dict[str, object] = {"collector": "gpu", "key": "gpu.count", "value": 2}
    data.update(overrides)
    return Fact(**data)  # type: ignore[arg-type]


def test_fact_defaults() -> None:
    fact = make_fact()
    assert fact.status is FactStatus.OK
    assert fact.timestamp.tzinfo is not None
    assert fact.source_command is None


@pytest.mark.parametrize("missing", ["collector", "key"])
def test_fact_requires_collector_and_key(missing: str) -> None:
    data = {"collector": "gpu", "key": "gpu.count"}
    del data[missing]
    with pytest.raises(ValidationError):
        Fact(**data)


def test_fact_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        make_fact(status="great")


def test_fact_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        make_fact(surprise=1)


def test_fact_is_frozen() -> None:
    fact = make_fact()
    with pytest.raises(ValidationError):
        fact.value = 3  # type: ignore[misc]


def test_fact_json_round_trip() -> None:
    fact = make_fact(
        value="595.91.07",
        unit=None,
        source_command="nvidia-smi",
        raw_excerpt="Driver Version: 595.91.07",
    )
    assert Fact.model_validate_json(fact.model_dump_json()) == fact


def test_to_shareable_drops_internal_fields() -> None:
    fact = make_fact(
        source_command="nvidia-smi --query-gpu=name", raw_excerpt="secret line"
    )
    shareable = fact.to_shareable()
    dumped = shareable.model_dump_json()
    assert "source_command" not in dumped
    assert "raw_excerpt" not in dumped
    assert "secret line" not in dumped
    assert "nvidia-smi" not in dumped
    assert set(ShareableFact.model_fields) == {
        "collector",
        "key",
        "value",
        "unit",
        "status",
        "timestamp",
    }


def test_shareable_rejects_long_string() -> None:
    with pytest.raises(ValidationError):
        make_fact(value="x" * 201).to_shareable()


@pytest.mark.parametrize("bad", ["line1\nline2", "esc\x1b[31m", "nul\x00", "del\x7f"])
def test_shareable_rejects_control_characters(bad: str) -> None:
    with pytest.raises(ValidationError):
        make_fact(value=bad).to_shareable()


def test_shareable_accepts_plain_values() -> None:
    for value in ("ok", 3, 1.5, True, None):
        assert make_fact(value=value).to_shareable().value == value


def test_finding_valid_and_round_trip() -> None:
    finding = Finding(
        rule_id="dns.resolve_failed",
        severity=Severity.CRITICAL,
        category=Category.OFFLINE,
        summary="DNS lookup for vast.ai failed",
        evidence=["network.dns.vast_ai"],
        recommendation="Check the resolver configuration.",
        fix_id="fix-dns-resolver",
    )
    assert finding.status is CheckStatus.FAIL
    assert Finding.model_validate_json(finding.model_dump_json()) == finding


def test_finding_rejects_bad_severity_and_empty_summary() -> None:
    base = {"rule_id": "r1", "category": "offline", "summary": "s"}
    with pytest.raises(ValidationError):
        Finding(**base, severity="urgent")
    with pytest.raises(ValidationError):
        Finding(**{**base, "summary": ""}, severity="critical")


def test_check_status_has_unknown_and_skipped() -> None:
    assert {s.value for s in CheckStatus} == {"pass", "fail", "unknown", "skipped"}
