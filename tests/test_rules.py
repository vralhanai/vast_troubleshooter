import random
from pathlib import Path

import pytest

from vast_troubleshooter.collectors import run_collectors
from vast_troubleshooter.collectors.driver import DriverCollector
from vast_troubleshooter.models import Category, CheckStatus, Fact, FactStatus, Severity
from vast_troubleshooter.rule_engine import (
    MAX_DEPTH,
    MAX_FILE_BYTES,
    RuleFileError,
    evaluate_rules,
    facts_by_key,
    findings_of,
    load_default_rules,
    load_rules_dir,
    load_rules_text,
)
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import fake_context

RULES = {r.id: r for r in load_default_rules()}


def F(key: str, value: object = None, status: FactStatus = FactStatus.OK) -> Fact:
    return Fact(collector=key.split(".")[0], key=key, value=value, status=status)


def run(rule_id: str, **facts: object) -> CheckStatus:
    """Evaluate one shipped rule. Keys use '__' for '.'."""
    built = [F(k.replace("__", "."), v) for k, v in facts.items()]
    (result,) = evaluate_rules([RULES[rule_id]], built)
    return result.status


# One failing and one passing fact set per shipped rule. Boundaries included.
CASES: dict[str, tuple[dict[str, object], dict[str, object]]] = {
    "network.dns_vast_ai_unresolvable": (
        {"network.dns_vast_ai_resolves": False},
        {"network.dns_vast_ai_resolves": True},
    ),
    "network.outbound_tcp_blocked": (
        {
            "network.outbound_probes_enabled": True,
            "network.outbound_cloudflare_443": False,
            "network.outbound_google_dns_53": False,
        },
        {
            "network.outbound_probes_enabled": True,
            "network.outbound_cloudflare_443": True,
            "network.outbound_google_dns_53": False,
        },
    ),
    "docker.nvidia_runtime_missing": (
        {"docker.nvidia_runtime_present": False},
        {"docker.nvidia_runtime_present": True},
    ),
    "docker.daemon_not_running": (
        {"docker.daemon_running": False},
        {"docker.daemon_running": True},
    ),
    "vast_host.service_not_active": (
        {"vast_host.service_active": False},
        {"vast_host.service_active": True},
    ),
    "kernel_log.xid_errors": (
        {"kernel_log.xid_count": 1},
        {"kernel_log.xid_count": 0},
    ),
    "kernel_log.xid_79_gpu_fell_off_bus": (
        {"kernel_log.xid_count": 3, "kernel_log.xid_codes": "13,79"},
        {"kernel_log.xid_count": 3, "kernel_log.xid_codes": "13,179"},
    ),
    "gpu.nvidia_smi_failing": (
        {"driver.nvidia_smi_responding": False},
        {"driver.nvidia_smi_responding": True},
    ),
    "gpu.count_below_pci_count": (
        {"gpu.count": 7, "gpu.pci_count": 8},
        {"gpu.count": 8, "gpu.pci_count": 8},
    ),
    "driver.cuda_below_11_8": (
        {"driver.cuda_version_code": 11070},
        {"driver.cuda_version_code": 11080},
    ),
    "disk.root_free_below_20gb": (
        {"disk.root_free_bytes": 19_999_999_999},
        {"disk.root_free_bytes": 20_000_000_000},
    ),
    "disk.docker_storage_below_200gb": (
        {"disk.docker_dir_total_bytes": 199_999_999_999},
        {"disk.docker_dir_total_bytes": 200_000_000_000},
    ),
    "disk.root_not_writable": (
        {"disk.root_mounted_rw": False},
        {"disk.root_mounted_rw": True},
    ),
    "disk.smart_health_failed": (
        {"disk.smart_failed_count": 1},
        {"disk.smart_failed_count": 0},
    ),
    "disk.nvme_unhealthy": (
        {"disk.nvme_critical_warning_count": 0, "disk.nvme_media_errors_total": 12},
        {"disk.nvme_critical_warning_count": 0, "disk.nvme_media_errors_total": 0},
    ),
    "disk.sata_bad_sectors": (
        {"disk.sata_reallocated_total": 0, "disk.sata_pending_total": 8},
        {"disk.sata_reallocated_total": 0, "disk.sata_pending_total": 0},
    ),
}


def status_of(rule_id: str, values: dict[str, object]) -> CheckStatus:
    (result,) = evaluate_rules([RULES[rule_id]], [F(k, v) for k, v in values.items()])
    return result.status


def test_every_shipped_rule_has_a_case_and_vice_versa() -> None:
    assert set(CASES) == set(RULES)


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_rule_fails_on_the_failing_facts(rule_id: str) -> None:
    assert status_of(rule_id, CASES[rule_id][0]) is CheckStatus.FAIL


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_rule_passes_on_the_passing_facts(rule_id: str) -> None:
    assert status_of(rule_id, CASES[rule_id][1]) is CheckStatus.PASS


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_rule_is_unknown_without_data_never_pass(rule_id: str) -> None:
    assert status_of(rule_id, {}) is CheckStatus.UNKNOWN


@pytest.mark.parametrize(
    "status",
    [
        FactStatus.ERROR,
        FactStatus.TIMEOUT,
        FactStatus.MISSING,
        FactStatus.NEEDS_ROOT,
        FactStatus.UNSUPPORTED,
    ],
)
def test_a_fact_that_could_not_be_read_makes_the_rule_unknown(
    status: FactStatus,
) -> None:
    fact = F("kernel_log.xid_count", None, status)
    (result,) = evaluate_rules([RULES["kernel_log.xid_errors"]], [fact])
    assert result.status is CheckStatus.UNKNOWN
    assert result.reasons == (f"kernel_log.xid_count: {status.value}",)
    assert result.finding is None


def test_outbound_rule_is_skipped_when_probes_were_not_run() -> None:
    values = {
        "network.outbound_probes_enabled": False,
        "network.outbound_cloudflare_443": False,
    }
    assert status_of("network.outbound_tcp_blocked", values) is CheckStatus.SKIPPED


def test_xid_79_rule_is_decided_when_there_are_no_xids_even_without_the_code_list() -> (
    None
):
    # No Xid at all means no Xid 79, even though xid_codes is not emitted then.
    assert (
        status_of("kernel_log.xid_79_gpu_fell_off_bus", {"kernel_log.xid_count": 0})
        is CheckStatus.PASS
    )


def test_xid_79_rule_stays_unknown_when_the_log_could_not_be_read() -> None:
    fact = F("kernel_log.xid_count", None, FactStatus.NEEDS_ROOT)
    (result,) = evaluate_rules([RULES["kernel_log.xid_79_gpu_fell_off_bus"]], [fact])
    assert result.status is CheckStatus.UNKNOWN


def test_findings_carry_rule_data_and_evidence_keys() -> None:
    (result,) = evaluate_rules(
        [RULES["gpu.count_below_pci_count"]],
        [F("gpu.count", 7), F("gpu.pci_count", 8)],
    )
    finding = result.finding
    assert finding is not None
    assert finding.rule_id == "gpu.count_below_pci_count"
    assert finding.status is CheckStatus.FAIL
    assert finding.severity is Severity.WARNING
    assert finding.category is Category.OFFLINE
    assert finding.evidence == ["gpu.count", "gpu.pci_count"]
    assert finding.recommendation
    dns = evaluate_rules(
        [RULES["network.dns_vast_ai_unresolvable"]],
        [F("network.dns_vast_ai_resolves", False)],
    )[0].finding
    assert dns is not None and dns.fix_id == "fix-dns-resolver"


def test_findings_are_sorted_most_severe_first_then_by_id() -> None:
    facts = [
        F("kernel_log.xid_count", 2),
        F("kernel_log.xid_codes", "79"),
        F("disk.root_mounted_rw", False),
    ]
    findings = findings_of(evaluate_rules(list(RULES.values()), facts))
    assert [f.rule_id for f in findings] == [
        "disk.root_not_writable",
        "kernel_log.xid_79_gpu_fell_off_bus",
        "kernel_log.xid_errors",
    ]
    assert [f.severity for f in findings] == [
        Severity.CRITICAL,
        Severity.CRITICAL,
        Severity.WARNING,
    ]


def test_evaluation_is_deterministic_for_any_fact_order() -> None:
    facts = [F(k, v) for failing, _ in CASES.values() for k, v in failing.items()]
    rules = list(RULES.values())
    baseline = evaluate_rules(rules, facts)
    for seed in range(5):
        shuffled = facts[:]
        random.Random(seed).shuffle(shuffled)
        assert evaluate_rules(rules, shuffled) == baseline


# -- three-valued logic and condition types ----------------------------------------


def rule_text(when: str, extra: str = "") -> str:
    return f"""
schema_version = 1
[[rule]]
id = "t.rule"
title = "t"
severity = "info"
category = "info"
source = "test"
checked = "2026-01-01"
{extra}
{when}
"""


def check(when: str, *facts: Fact, extra: str = "") -> CheckStatus:
    rules = load_rules_text(rule_text(when, extra))
    return evaluate_rules(rules, facts)[0].status


def leaf(fact: str, op: str, value: str) -> str:
    return f'{{ type = "{op}", fact = "{fact}", value = {value} }}'


def test_all_is_false_if_any_part_is_false_even_when_another_is_unknown() -> None:
    when = f'when = {{ type = "all", of = [{leaf("a", "equals", "true")}, {leaf("b", "equals", "true")}] }}'
    assert check(when, F("a", False)) is CheckStatus.PASS
    assert check(when, F("a", True)) is CheckStatus.UNKNOWN
    assert check(when, F("a", True), F("b", True)) is CheckStatus.FAIL


def test_any_is_true_if_any_part_is_true_even_when_another_is_unknown() -> None:
    when = f'when = {{ type = "any", of = [{leaf("a", "equals", "true")}, {leaf("b", "equals", "true")}] }}'
    assert check(when, F("a", True)) is CheckStatus.FAIL
    assert check(when, F("a", False)) is CheckStatus.UNKNOWN
    assert check(when, F("a", False), F("b", False)) is CheckStatus.PASS


@pytest.mark.parametrize(
    ("op", "value", "actual", "expected"),
    [
        ("equals", "5", 5, CheckStatus.FAIL),
        ("equals", "5", 6, CheckStatus.PASS),
        ("not_equals", "5", 6, CheckStatus.FAIL),
        ("not_equals", "5", 5, CheckStatus.PASS),
        ("less_than", "5", 4, CheckStatus.FAIL),
        ("less_than", "5", 5, CheckStatus.PASS),
        ("greater_than", "5", 6, CheckStatus.FAIL),
        ("greater_than", "5", 5, CheckStatus.PASS),
        ("greater_than", "1.5", 2, CheckStatus.FAIL),
        ("equals", '"abc"', "abc", CheckStatus.FAIL),
    ],
)
def test_comparisons(
    op: str, value: str, actual: object, expected: CheckStatus
) -> None:
    assert check(f"when = {leaf('a', op, value)}", F("a", actual)) is expected


@pytest.mark.parametrize(
    ("op", "value", "actual"),
    [
        ("less_than", "5", "text"),  # not a number
        ("less_than", "5", True),  # a bool is not a number
        ("equals", "true", 1),  # bool vs int
        ("equals", "5", "5"),  # int vs str
    ],
)
def test_type_mismatch_is_unknown_not_a_guess(
    op: str, value: str, actual: object
) -> None:
    assert (
        check(f"when = {leaf('a', op, value)}", F("a", actual)) is CheckStatus.UNKNOWN
    )


def test_contains_matches_whole_tokens_only() -> None:
    when = f"when = {leaf('codes', 'contains', chr(34) + '79' + chr(34))}"
    assert check(when, F("codes", "13,79,31")) is CheckStatus.FAIL
    assert check(when, F("codes", "179")) is CheckStatus.PASS
    assert check(when, F("codes", 79)) is CheckStatus.UNKNOWN  # not text


def test_missing_condition() -> None:
    when = 'when = { type = "missing", fact = "a" }'
    assert check(when) is CheckStatus.FAIL  # absent
    assert (
        check(when, F("a", None, FactStatus.MISSING)) is CheckStatus.FAIL
    )  # tool not installed
    assert check(when, F("a", 1)) is CheckStatus.PASS
    assert check(when, F("a", None, FactStatus.ERROR)) is CheckStatus.UNKNOWN
    assert check(when, F("a", None, FactStatus.NEEDS_ROOT)) is CheckStatus.UNKNOWN


def test_comparing_two_facts() -> None:
    when = 'when = { type = "less_than", fact = "a", other_fact = "b" }'
    assert check(when, F("a", 1), F("b", 2)) is CheckStatus.FAIL
    assert check(when, F("a", 2), F("b", 2)) is CheckStatus.PASS
    assert check(when, F("a", 1)) is CheckStatus.UNKNOWN


# -- the loader rejects anything outside the closed schema --------------------------


def bad(text: str, match: str = "") -> None:
    with pytest.raises(RuleFileError, match=match or None):
        load_rules_text(text)


@pytest.mark.parametrize("kind", ["eval", "python", "exec", "shell", "regex", "EQUALS"])
def test_unknown_condition_types_are_rejected_so_rules_cannot_run_code(
    kind: str,
) -> None:
    bad(rule_text(f'when = {{ type = "{kind}", fact = "a", value = "x" }}'), "")


def test_unknown_fields_are_rejected() -> None:
    bad(
        rule_text(
            'when = { type = "missing", fact = "a" }', extra='command = "rm -rf /"'
        ),
        "command",
    )
    bad(rule_text('when = { type = "missing", fact = "a", also = 1 }'), "also")


def test_malformed_toml_is_rejected() -> None:
    bad("this is [not toml", "invalid TOML")


def test_wrong_schema_version_is_rejected() -> None:
    text = rule_text('when = { type = "missing", fact = "a" }').replace(
        "schema_version = 1", "schema_version = 2"
    )
    bad(text, "schema_version")


def test_missing_required_field_is_rejected() -> None:
    text = rule_text('when = { type = "missing", fact = "a" }').replace(
        'source = "test"', ""
    )
    bad(text, "source")


def test_empty_file_and_empty_rule_list_are_rejected() -> None:
    bad("", "")
    bad("schema_version = 1\n", "")


def test_bad_values_are_rejected() -> None:
    base = rule_text('when = { type = "missing", fact = "a" }')
    bad(base.replace('severity = "info"', 'severity = "urgent"'), "severity")
    bad(base.replace('id = "t.rule"', 'id = "Bad Id!"'), "id")
    bad(base.replace('checked = "2026-01-01"', 'checked = "yesterday"'), "checked")
    bad(rule_text('when = { type = "missing", fact = "a b" }'), "fact")
    bad(
        rule_text('when = { type = "missing", fact = "a" }', extra='fix_id = "rm -rf"'),
        "fix_id",
    )


def test_comparison_needs_exactly_one_operand() -> None:
    bad(rule_text('when = { type = "less_than", fact = "a" }'), "exactly one")
    bad(
        rule_text(
            'when = { type = "less_than", fact = "a", value = 1, other_fact = "b" }'
        ),
        "exactly one",
    )


def test_numeric_operators_reject_text_values() -> None:
    bad(rule_text('when = { type = "less_than", fact = "a", value = "ten" }'), "")


def test_nesting_and_size_are_bounded() -> None:
    inner = '{ type = "missing", fact = "a" }'
    for _ in range(MAX_DEPTH):
        inner = f'{{ type = "all", of = [{inner}] }}'
    bad(rule_text(f"when = {inner}"), "deeper")
    many = ", ".join(['{ type = "missing", fact = "a" }'] * 20)
    wide = f'{{ type = "all", of = [{many}] }}'
    nested = f'{{ type = "all", of = [{wide}, {wide}, {wide}] }}'
    bad(rule_text(f"when = {nested}"), "more than")
    bad(
        rule_text('when = { type = "missing", fact = "a" }')
        + "# "
        + "x" * MAX_FILE_BYTES,
        "larger",
    )


def test_unknown_fix_ids_are_rejected_when_a_registry_is_given() -> None:
    text = rule_text(
        'when = { type = "missing", fact = "a" }', extra='fix_id = "do-things"'
    )
    assert load_rules_text(text)[0].fix_id == "do-things"  # only a name
    with pytest.raises(RuleFileError, match="unknown fix"):
        load_rules_text(text, known_fix_ids=["restart-vast-daemon"])
    assert load_rules_text(text, known_fix_ids=["do-things"])


def test_duplicate_ids_across_files_are_rejected(tmp_path: Path) -> None:
    text = rule_text('when = { type = "missing", fact = "a" }')
    (tmp_path / "one.toml").write_text(text)
    (tmp_path / "two.toml").write_text(text)
    with pytest.raises(RuleFileError, match="duplicate"):
        load_rules_dir(tmp_path)


def test_one_bad_file_loads_nothing(tmp_path: Path) -> None:
    (tmp_path / "a_good.toml").write_text(
        rule_text('when = { type = "missing", fact = "a" }')
    )
    (tmp_path / "b_bad.toml").write_text("nope [")
    with pytest.raises(RuleFileError, match="b_bad.toml"):
        load_rules_dir(tmp_path)


# -- the shipped rules ---------------------------------------------------------------


def test_every_shipped_rule_names_its_source_and_check_date() -> None:
    for rule in RULES.values():
        assert rule.source.startswith("https://docs.vast.ai/")
        assert rule.checked == "2026-11-23"
        assert rule.recommendation


def test_shipped_rules_load_in_a_stable_order() -> None:
    assert [r.id for r in load_default_rules()] == [r.id for r in load_default_rules()]


def test_shipped_fix_ids_are_only_the_two_planned_fixes() -> None:
    assert {r.fix_id for r in RULES.values() if r.fix_id} == {
        "fix-dns-resolver",
        "restart-vast-daemon",
    }


def test_thresholds_live_in_the_rule_files_not_in_code() -> None:
    import vast_troubleshooter.rule_engine as engine

    source = Path(engine.__file__).read_text()
    for number in ("11080", "20_000_000_000", "200_000_000_000"):
        assert number not in source


# -- facts from real collectors flow through the rules --------------------------------


def smi(cuda: str) -> CommandResult:
    text = (
        f"| NVIDIA-SMI 580.178.04  Driver Version: 580.178.04  CUDA Version: {cuda} |"
    )
    return CommandResult(
        "driver.smi_summary", CommandStatus.OK, returncode=0, stdout=text
    )


def driver_findings(cuda: str) -> list[str]:
    ctx = fake_context(results={"driver.smi_summary": smi(cuda)})
    facts = run_collectors([DriverCollector()], ctx)
    return [f.rule_id for f in findings_of(evaluate_rules(list(RULES.values()), facts))]


def test_collector_output_drives_the_cuda_rule() -> None:
    assert driver_findings("11.7") == ["driver.cuda_below_11_8"]
    assert driver_findings("11.8") == []
    assert driver_findings("12.6") == []


def test_a_broken_nvidia_smi_gives_a_finding_not_a_pass() -> None:
    failed = CommandResult(
        "driver.smi_summary",
        CommandStatus.ERROR,
        returncode=9,
        stdout="NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.",
    )
    facts = run_collectors(
        [DriverCollector()], fake_context(results={"driver.smi_summary": failed})
    )
    results = evaluate_rules(list(RULES.values()), facts)
    by_id = {r.rule_id: r for r in results}
    assert by_id["gpu.nvidia_smi_failing"].status is CheckStatus.FAIL
    # The CUDA version could not be read, so that rule is unknown, not passing.
    assert by_id["driver.cuda_below_11_8"].status is CheckStatus.UNKNOWN


def test_facts_by_key_last_one_wins() -> None:
    assert facts_by_key([F("a", 1), F("a", 2)])["a"].value == 2


# -- a broken installation must never look like an all-clear (F-08) ----------------


def test_a_directory_with_no_rules_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuleFileError, match="no rules"):
        load_rules_dir(tmp_path)


def test_a_missing_rule_directory_is_a_rule_file_error(tmp_path: Path) -> None:
    with pytest.raises(RuleFileError, match="cannot read"):
        load_rules_dir(tmp_path / "does-not-exist")


def test_a_rule_file_that_is_not_utf8_is_a_rule_file_error(tmp_path: Path) -> None:
    (tmp_path / "a.toml").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(RuleFileError, match="a.toml"):
        load_rules_dir(tmp_path)
