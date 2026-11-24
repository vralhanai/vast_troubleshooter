from pathlib import Path

import pytest

from vast_troubleshooter.collectors import COLLECTORS
from vast_troubleshooter.collectors.kernel_log import (
    COUNT_KEYS,
    KernelLogCollector,
    analyze,
    line_time,
)
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.operations import READ_OPERATIONS
from vast_troubleshooter.runner import CommandResult, CommandStatus
from vast_troubleshooter.testing import FakeRunner, HostContext, fake_context

FIXTURES = Path(__file__).parent / "fixtures" / "kernel_log"
JOURNAL = "kernel.journal"
DMESG = "kernel.dmesg"


def text(name: str) -> str:
    return (FIXTURES / name).read_text()


def ok(op: str, body: str, truncated: bool = False) -> CommandResult:
    return CommandResult(
        op, CommandStatus.OK, returncode=0, stdout=body, truncated=truncated
    )


def bad(
    op: str, status: CommandStatus, stdout: str = "", detail: str = ""
) -> CommandResult:
    return CommandResult(op, status, returncode=1, stdout=stdout, detail=detail)


def collect(results: dict[str, CommandResult]) -> tuple[dict[str, Fact], FakeRunner]:
    runner = FakeRunner(results)
    ctx = HostContext(runner=runner, files=fake_context().files)
    facts = KernelLogCollector().collect(ctx)
    return {f.key: f for f in facts}, runner


def journal(name: str) -> dict[str, Fact]:
    return collect({JOURNAL: ok(JOURNAL, text(name))})[0]


# -- Xid ------------------------------------------------------------------------


def test_xid_79_log() -> None:
    facts = journal("xid79_journal.txt")
    assert facts["kernel_log.source"].value == "journalctl_current_boot"
    assert facts["kernel_log.xid_count"].value == 3  # the plain NVRM line is not an Xid
    assert facts["kernel_log.xid_codes"].value == "13,79"
    assert facts["kernel_log.xid.79.count"].value == 2
    assert facts["kernel_log.xid.13.count"].value == 1
    assert facts["kernel_log.xid_device_count"].value == 2
    assert facts["kernel_log.xid_first_seen"].value == "2026-11-23T10:15:42"
    assert facts["kernel_log.xid_last_seen"].value == "2026-11-23T11:00:00"
    assert facts["kernel_log.truncated"].value is False


# -- AER ------------------------------------------------------------------------


def test_aer_events_are_counted_once_per_event_by_severity() -> None:
    facts = journal("aer_journal.txt")
    assert facts["kernel_log.aer_corrected_count"].value == 2
    assert facts["kernel_log.aer_uncorrected_count"].value == 1
    assert facts["kernel_log.aer_fatal_count"].value == 1
    assert facts["kernel_log.aer_last_seen"].value == "2026-11-23T09:45:00"
    assert facts["kernel_log.xid_count"].value == 0


def test_aer_falls_back_to_severity_lines_when_there_are_no_headers() -> None:
    facts = journal("aer_severity_only_journal.txt")
    assert facts["kernel_log.aer_corrected_count"].value == 1
    assert facts["kernel_log.aer_fatal_count"].value == 1
    assert facts["kernel_log.aer_uncorrected_count"].value == 0


# -- clean log ------------------------------------------------------------------


def test_clean_journal_when_journalctl_exits_non_zero_with_no_entries() -> None:
    facts, _ = collect(
        {JOURNAL: bad(JOURNAL, CommandStatus.ERROR, stdout=text("clean_journal.txt"))}
    )
    for key in COUNT_KEYS:
        assert facts[key].value == 0
        assert facts[key].status is FactStatus.OK
    assert facts["kernel_log.source"].value == "journalctl_current_boot"
    assert "kernel_log.xid_codes" not in facts  # nothing to report
    assert "kernel_log.xid_last_seen" not in facts


def test_clean_journal_with_exit_zero() -> None:
    facts, _ = collect({JOURNAL: ok(JOURNAL, "")})
    assert facts["kernel_log.xid_count"].value == 0


def test_clean_dmesg_has_zero_counts() -> None:
    facts, _ = collect(
        {
            JOURNAL: bad(JOURNAL, CommandStatus.MISSING),
            DMESG: ok(DMESG, text("clean_dmesg.txt")),
        }
    )
    assert facts["kernel_log.source"].value == "dmesg"
    assert facts["kernel_log.xid_count"].value == 0
    assert (
        facts["kernel_log.io_error_count"].value == 0
    )  # 'NVRM: loading' is not an Xid


# -- dmesg fallback and I/O errors ----------------------------------------------


def test_falls_back_to_dmesg_when_journalctl_is_missing() -> None:
    facts, runner = collect(
        {
            JOURNAL: bad(JOURNAL, CommandStatus.MISSING),
            DMESG: ok(DMESG, text("xid79_dmesg.txt")),
        }
    )
    assert [c[0] for c in runner.calls] == [JOURNAL, DMESG]
    assert facts["kernel_log.source"].value == "dmesg"
    assert facts["kernel_log.xid_count"].value == 1
    assert facts["kernel_log.xid.79.count"].value == 1
    assert facts["kernel_log.xid_first_seen"].value == "2026-11-03T10:15:42"
    assert facts["kernel_log.io_error_count"].value == 3
    assert facts["kernel_log.io_error_last_seen"].value == "2026-11-23T10:21:00"


@pytest.mark.parametrize("status", [CommandStatus.ERROR, CommandStatus.TIMEOUT])
def test_falls_back_to_dmesg_when_journalctl_fails_or_hangs(
    status: CommandStatus,
) -> None:
    facts, _ = collect(
        {
            JOURNAL: bad(JOURNAL, status, stdout="boom"),
            DMESG: ok(DMESG, text("xid79_dmesg.txt")),
        }
    )
    assert facts["kernel_log.source"].value == "dmesg"


# -- cannot read ----------------------------------------------------------------


def test_needs_root_is_reported_and_dmesg_is_not_tried() -> None:
    facts, runner = collect(
        {
            JOURNAL: CommandResult(
                JOURNAL,
                CommandStatus.NEEDS_ROOT,
                detail="needs root; elevated reads are not enabled",
            )
        }
    )
    assert [c[0] for c in runner.calls] == [JOURNAL]
    for key in (*COUNT_KEYS, "kernel_log.source"):
        assert facts[key].status is FactStatus.NEEDS_ROOT
        assert facts[key].value is None
    assert "needs root" in (facts["kernel_log.xid_count"].raw_excerpt or "")


def test_both_sources_missing() -> None:
    facts, _ = collect(
        {
            JOURNAL: bad(JOURNAL, CommandStatus.MISSING),
            DMESG: bad(DMESG, CommandStatus.MISSING),
        }
    )
    assert all(facts[k].status is FactStatus.MISSING for k in COUNT_KEYS)


def test_dmesg_hang_is_a_timeout() -> None:
    facts, _ = collect(
        {
            JOURNAL: bad(JOURNAL, CommandStatus.MISSING),
            DMESG: bad(DMESG, CommandStatus.TIMEOUT),
        }
    )
    assert all(facts[k].status is FactStatus.TIMEOUT for k in COUNT_KEYS)


def test_unreadable_log_never_looks_clean() -> None:
    facts, _ = collect({JOURNAL: CommandResult(JOURNAL, CommandStatus.NEEDS_ROOT)})
    assert not any(f.value == 0 for f in facts.values())


def test_truncated_output_is_flagged() -> None:
    facts, _ = collect(
        {JOURNAL: ok(JOURNAL, text("xid79_journal.txt"), truncated=True)}
    )
    assert facts["kernel_log.truncated"].value is True


# -- no raw log text in facts ---------------------------------------------------


def test_no_raw_log_text_reaches_any_fact() -> None:
    for name, op in (("xid79_journal.txt", JOURNAL), ("aer_journal.txt", JOURNAL)):
        facts, _ = collect({op: ok(op, text(name))})
        dumped = " ".join(f.model_dump_json() for f in facts.values())
        for leaked in (
            "gpu-host",
            "python3",
            "trainer",
            "pid=",
            "0000:3a:00",
            "fallen off",
        ):
            assert leaked not in dumped
    facts, _ = collect(
        {
            JOURNAL: bad(JOURNAL, CommandStatus.MISSING),
            DMESG: ok(DMESG, text("xid79_dmesg.txt")),
        }
    )
    dumped = " ".join(f.model_dump_json() for f in facts.values())
    for leaked in ("python3", "nvme0n1", "lcy02", "comm ls"):
        assert leaked not in dumped


def test_facts_are_safe_to_share() -> None:
    for name in ("xid79_journal.txt", "aer_journal.txt"):
        for fact in journal(name).values():
            fact.to_shareable()


# -- parsing helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2026-11-23T10:15:42+0000 host kernel: x", "2026-11-23T10:15:42"),
        ("[Mon Nov 23 10:15:42 2026] x", "2026-11-23T10:15:42"),
        ("[Mon Nov  3 10:15:42 2026] x", "2026-11-03T10:15:42"),
        ("[    5.123456] x", None),
        ("no time here", None),
    ],
)
def test_line_time(line: str, expected: str | None) -> None:
    assert line_time(line) == expected


def test_xid_pattern_ignores_lookalikes() -> None:
    found = analyze(
        "NVRM: GPU 0000:3a:00.0: GPU has fallen off the bus.\n"
        "xid is a word here\n"
        "NVRM: Xid (PCI:0000:3a:00): 31, pid=1, name=x, MMU Fault\n"
    )
    assert dict(found.xid_codes) == {31: 1}


# -- operation definitions ------------------------------------------------------


def test_kernel_operations_need_root_and_never_page() -> None:
    journal_op = READ_OPERATIONS[JOURNAL]
    dmesg_op = READ_OPERATIONS[DMESG]
    assert journal_op.needs_root and dmesg_op.needs_root
    assert "--no-pager" in journal_op.args
    assert journal_op.args[:2] == ("-k", "-b")
    assert dmesg_op.args == ("-T",)


def test_registry_includes_kernel_log() -> None:
    assert "kernel_log" in COLLECTORS


# -- cut-off output proves only what it contains (F-02) -------------------------


def _rules_for(facts: dict[str, Fact]) -> dict[str, str]:
    from vast_troubleshooter.rule_engine import evaluate_rules, load_default_rules

    return {
        r.rule_id: r.status.value
        for r in evaluate_rules(load_default_rules(), list(facts.values()))
    }


def test_truncated_clean_output_is_not_a_clean_bill() -> None:
    facts, _ = collect({JOURNAL: ok(JOURNAL, "boot line\n", truncated=True)})
    for key in COUNT_KEYS:
        assert facts[key].status is FactStatus.ERROR and facts[key].value is None
    rules = _rules_for(facts)
    assert rules["kernel_log.xid_errors"] == "unknown"  # was "pass"
    assert rules["kernel_log.xid_79_gpu_fell_off_bus"] == "unknown"  # was "pass"


def test_truncated_output_with_other_xids_cannot_rule_out_79() -> None:
    body = "NVRM: Xid (PCI:0000:01:00): 13, Graphics exception\n"
    facts, _ = collect({JOURNAL: ok(JOURNAL, body, truncated=True)})
    assert facts["kernel_log.xid_count"].value == 1  # real events still count
    assert facts["kernel_log.xid_codes"].status is FactStatus.ERROR
    rules = _rules_for(facts)
    assert rules["kernel_log.xid_errors"] == "fail"
    assert rules["kernel_log.xid_79_gpu_fell_off_bus"] == "unknown"


def test_truncated_output_that_did_show_xid_79_still_fails() -> None:
    facts, _ = collect(
        {JOURNAL: ok(JOURNAL, text("xid79_journal.txt"), truncated=True)}
    )
    assert _rules_for(facts)["kernel_log.xid_79_gpu_fell_off_bus"] == "fail"


def test_complete_clean_output_still_passes() -> None:
    facts, _ = collect({JOURNAL: ok(JOURNAL, "boot line\n")})
    assert _rules_for(facts)["kernel_log.xid_79_gpu_fell_off_bus"] == "pass"
