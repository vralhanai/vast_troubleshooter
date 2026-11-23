"""A report an operator can attach to a Vast support request.

Protection comes in layers:

1. The schema is an ALLOWLIST of typed fields. Raw log lines, command lines,
   `raw_excerpt`, and `source_command` have no place in it, so they cannot be
   written even by mistake.
2. Every string is then redacted (addresses, host and user names, machine ids,
   keys and tokens). There is no way to turn this off.
3. A final guard re-checks the finished text, and refuses to write anything if
   a second redaction pass would still change it.
4. Markdown is rendered as inert text: nothing from the host can become a link,
   HTML, a heading, or terminal control codes.

Files are created owner-only, never through a symlink, and never overwrite an
existing file. Nothing is uploaded.
"""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vast_troubleshooter import __version__
from vast_troubleshooter.diagnose import Diagnosis
from vast_troubleshooter.models import Fact, FactStatus, Scalar, ShareableFact
from vast_troubleshooter.redact import Redactor
from vast_troubleshooter.securefs import create_private_file, ensure_private_dir

REPORT_SCHEMA_VERSION = 1
REDACTION_VERSION = 1
MAX_ACTIONS = 50

# Only these keys of an action-log line are ever copied into a report.
_ACTION_KEYS = (
    "time", "run_id", "fix_id", "phase", "status", "block_code", "reasons",
    "commands", "rental_state", "before", "after", "rolled_back", "rollback_failed", "op_id", "returncode",
)  # fmt: skip


class ReportError(Exception):
    """The report could not be made safely. Nothing was written."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(_Closed):
    key: str
    value: Scalar = None
    status: str


class ReportFinding(_Closed):
    rule_id: str
    severity: str
    category: str
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    recommendation: str = ""
    fix_id: str | None = None


class ReportUnknown(_Closed):
    rule_id: str
    severity: str
    title: str = ""
    reasons: list[str] = Field(default_factory=list)


class HostSummary(_Closed):
    environment: str | None = None
    os: str | None = None
    kernel: str | None = None
    gpu_count: int | None = None
    gpu_model: str | None = None
    driver_version: str | None = None
    cuda_version: str | None = None
    docker_version: str | None = None
    uptime_seconds: float | None = None


class RentalSection(_Closed):
    state: str = "unknown"
    blocks_disruptive_fixes: bool | None = None
    reason: str | None = None
    unresolved_sources: str | None = None


class RunSummary(_Closed):
    exit_code: int
    complete: bool
    counts: dict[str, int]


class ReportAction(_Closed):
    time: str | None = None
    run_id: str | None = None
    fix_id: str | None = None
    phase: str | None = None
    status: str | None = None
    block_code: str | None = None
    reasons: list[str] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)
    rental_state: str | None = None
    before: str | None = None
    after: str | None = None
    rolled_back: bool | None = None
    rollback_failed: bool | None = None
    op_id: str | None = None
    returncode: int | None = None


class RedactionInfo(_Closed):
    applied: bool = True
    version: int = REDACTION_VERSION
    note: str = (
        "IP and MAC addresses, host and user names, machine ids, emails, keys, "
        "and tokens were removed. Review this file before sharing it."
    )


class Report(_Closed):
    schema_version: int = REPORT_SCHEMA_VERSION
    generated_at: str
    tool_version: str
    redaction: RedactionInfo = Field(default_factory=RedactionInfo)
    summary: RunSummary
    host: HostSummary
    rental: RentalSection
    findings: list[ReportFinding]
    unknown: list[ReportUnknown]
    facts: list[ShareableFact]
    actions: list[ReportAction]


# -- building ------------------------------------------------------------------------------


def _ok(facts: Mapping[str, Fact], key: str) -> Any:
    fact = facts.get(key)
    return fact.value if fact is not None and fact.status is FactStatus.OK else None


def _host(facts: Mapping[str, Fact]) -> HostSummary:
    os_name = _ok(facts, "system.os_pretty_name")
    return HostSummary(
        environment=_str(_ok(facts, "system.environment")),
        os=os_name if isinstance(os_name, str) else None,
        kernel=_str(_ok(facts, "system.kernel_release")),
        gpu_count=_int(_ok(facts, "gpu.count")),
        gpu_model=_str(_ok(facts, "gpu.0.name")),
        driver_version=_str(_ok(facts, "driver.version")),
        cuda_version=_str(_ok(facts, "driver.cuda_version")),
        docker_version=_str(_ok(facts, "docker.server_version")),
        uptime_seconds=_float(_ok(facts, "system.uptime_seconds")),
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def _rental(facts: Mapping[str, Fact]) -> RentalSection:
    blocks = _ok(facts, "rentals.blocks_disruptive_fixes")
    return RentalSection(
        state=_str(_ok(facts, "rentals.state")) or "unknown",
        blocks_disruptive_fixes=blocks if isinstance(blocks, bool) else None,
        reason=_str(_ok(facts, "rentals.reason")),
        unresolved_sources=_str(_ok(facts, "rentals.unresolved_sources")),
    )


def read_actions(path: Path | None, limit: int = MAX_ACTIONS) -> list[dict[str, Any]]:
    """The last `limit` action-log entries, keeping only allowlisted keys.
    Missing files, symlinks, and malformed lines yield nothing, never an error."""
    if path is None or path.is_symlink() or not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-limit * 2 :]:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict):
            entries.append({k: raw[k] for k in _ACTION_KEYS if k in raw})
    return entries[-limit:]


def _action(entry: Mapping[str, Any]) -> ReportAction | None:
    try:
        return ReportAction.model_validate(
            {k: v for k, v in entry.items() if k in _ACTION_KEYS}
        )
    except ValueError:
        return None  # a line that does not fit the schema is left out, not guessed at


def build_report(
    diagnosis: Diagnosis,
    actions: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> Report:
    facts = {f.key: f for f in diagnosis.facts}
    shareable: list[ShareableFact] = []
    for fact in diagnosis.facts:
        try:
            shareable.append(fact.to_shareable())
        except ValueError:
            continue  # a value that is not safe to share is simply left out

    def evidence(key: str) -> Evidence:
        fact = facts.get(key)
        if fact is None:
            return Evidence(key=key, status="not_collected")
        return Evidence(key=key, value=fact.value, status=fact.status.value)

    return Report(
        generated_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
        tool_version=__version__,
        summary=RunSummary(
            exit_code=diagnosis.exit_code,
            complete=not diagnosis.incomplete,
            counts=diagnosis.counts(),
        ),
        host=_host(facts),
        rental=_rental(facts),
        findings=[
            ReportFinding(
                rule_id=f.rule_id,
                severity=f.severity.value,
                category=f.category.value,
                summary=f.summary,
                evidence=[evidence(k) for k in f.evidence],
                recommendation=f.recommendation,
                fix_id=f.fix_id,
            )
            for f in diagnosis.findings
        ],
        unknown=[
            ReportUnknown(
                rule_id=r.rule_id,
                severity=r.severity.value,
                title=r.title,
                reasons=list(r.reasons),
            )
            for r in diagnosis.unknown
        ],
        facts=shareable,
        actions=[a for e in (actions or []) if (a := _action(e)) is not None],
    )


# -- redaction and rendering ------------------------------------------------------------------


def redact_report(report: Report, redactor: Redactor) -> Report:
    """Redact every string in the report, then check it is still a valid report."""
    return Report.model_validate(redactor.obj(report.model_dump(mode="json")))


def render_json(report: Report) -> str:
    return (
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=True) + "\n"
    )


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#|~!&+])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def md(value: object) -> str:
    """Make any value inert Markdown text. No link, HTML, heading, emphasis, or
    control character can survive, so host text can never format the report."""
    text = _CONTROL.sub(" ", "" if value is None else str(value))
    text = text.replace("://", ":\\/\\/")  # no automatic links
    return _MD_SPECIAL.sub(r"\\\1", text)


def render_markdown(report: Report) -> str:
    out: list[str] = []
    add = out.append
    add("# Vast Troubleshooter report")
    add("")
    add(f"Generated: {md(report.generated_at)}  ")
    add(f"Tool version: {md(report.tool_version)}  ")
    add("Produced locally. Nothing was uploaded.")
    add("")
    add(f"Redaction: {md(report.redaction.note)}")
    add("")
    s = report.summary
    c = s.counts
    add("## Summary")
    add("")
    add(f"- Diagnosis: {'complete' if s.complete else 'INCOMPLETE'}")
    add(f"- Exit code: {s.exit_code}")
    add(
        f"- Checks: {c.get('fail', 0)} problem\\(s\\), {c.get('pass', 0)} ok, "
        f"{c.get('unknown', 0)} could not be checked, {c.get('skipped', 0)} not applicable"
    )
    add("")
    add("## Host")
    add("")
    for label, value in (
        ("Environment", report.host.environment),
        ("OS", report.host.os), ("Kernel", report.host.kernel),
        ("GPUs", report.host.gpu_count), ("GPU model", report.host.gpu_model),
        ("NVIDIA driver", report.host.driver_version), ("CUDA", report.host.cuda_version),
        ("Docker", report.host.docker_version), ("Uptime \\(seconds\\)", report.host.uptime_seconds),
    ):  # fmt: skip
        add(f"- {label}: {md(value) if value is not None else 'not collected'}")
    add("")
    add("## Rental state")
    add("")
    r = report.rental
    add(f"- State: {md(r.state)}")
    if r.blocks_disruptive_fixes is not None:
        add(
            f"- Disruptive fixes blocked: {'yes' if r.blocks_disruptive_fixes else 'no'}"
        )
    if r.reason:
        add(f"- Reason: {md(r.reason)}")
    if r.unresolved_sources:
        add(f"- Sources that could not look: {md(r.unresolved_sources)}")
    add("")
    add("## Findings")
    add("")
    if not report.findings:
        add("No problems were found in the checks that ran.")
        add("")
    for finding in report.findings:
        add(f"### {md(finding.severity.upper())}: {md(finding.summary)}")
        add("")
        add(f"- Rule: {md(finding.rule_id)}")
        add(f"- Category: {md(finding.category)}")
        for item in finding.evidence:
            value = "" if item.value is None else f" = {md(item.value)}"
            add(f"- Evidence: {md(item.key)}{value} \\({md(item.status)}\\)")
        if finding.recommendation:
            add(f"- What to do: {md(finding.recommendation)}")
        if finding.fix_id:
            add(f"- Related fix: {md(finding.fix_id)}")
        add("")
    add("## Could not be checked")
    add("")
    if not report.unknown:
        add("Every check could be decided.")
    for item in report.unknown:
        why = ", ".join(item.reasons) or "no data"
        add(f"- {md(item.title or item.rule_id)} \\({md(item.severity)}\\): {md(why)}")
    add("")
    add("## Fix actions")
    add("")
    if not report.actions:
        add("No fix has been run or attempted on this host by this tool.")
    for action in report.actions:
        bits = [
            action.time,
            action.fix_id,
            action.phase,
            action.status,
            action.block_code,
        ]
        line = " / ".join(md(b) for b in bits if b)
        why = "; ".join(action.reasons)
        add(f"- {line}" + (f": {md(why)}" if why else ""))
    add("")
    add("## Facts read")
    add("")
    for fact in report.facts:
        value = "" if fact.value is None else f" = {md(fact.value)}"
        unit = f" {md(fact.unit)}" if fact.unit and fact.value is not None else ""
        add(f"- {md(fact.key)}{value}{unit} \\({md(fact.status.value)}\\)")
    add("")
    return "\n".join(out)


# -- guard and writing --------------------------------------------------------------------------


def make_report_files(
    diagnosis: Diagnosis,
    redactor: Redactor,
    actions: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Build, redact, render, and check. Returns (json, markdown) or raises."""
    report = redact_report(build_report(diagnosis, actions, now), redactor)
    json_text = render_json(report)
    markdown = render_markdown(report)
    # The JSON is the authority. If a second pass would still change it, a
    # sensitive string survived the first, so nothing may be written.
    if redactor.leaves_something(json_text):
        raise ReportError(
            "redaction check failed: the report still looks sensitive; nothing was written"
        )
    return json_text, markdown


def write_report(
    directory: Path, json_text: str, markdown: str, now: datetime | None = None
) -> tuple[Path, Path]:
    """Create `report-<UTC timestamp>.json` and `.md` as NEW owner-only files.
    A name collision gets a numeric suffix; an existing file is never replaced."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    try:
        directory = ensure_private_dir(directory.expanduser())
    except OSError as exc:
        raise ReportError(f"cannot use the output directory: {exc}") from exc
    directory = directory.resolve()
    for n in range(100):
        suffix = "" if n == 0 else f"-{n}"
        json_path = directory / f"report-{stamp}{suffix}.json"
        md_path = directory / f"report-{stamp}{suffix}.md"
        if any(p.exists() or p.is_symlink() for p in (json_path, md_path)):
            continue
        try:
            create_private_file(json_path, json_text.encode("utf-8"))
            try:
                create_private_file(md_path, markdown.encode("utf-8"))
            except OSError:
                json_path.unlink(missing_ok=True)  # never leave half a report
                raise
        except OSError as exc:
            raise ReportError(f"cannot write the report: {exc}") from exc
        return json_path, md_path
    raise ReportError("too many reports with the same timestamp")
