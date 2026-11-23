"""The Vast host daemon (vastai.service) and its recent log.

Service state comes from `systemctl show`. The log is the last 100 lines of
kaalia.log, analysed for error lines and then dropped: only counts and the last
error time become facts, because the log can name customers, containers, and
addresses.

The log format is NOT documented by Vast and has not been seen on a real host.
The error detection here is deliberately generic (words like "error" and
"traceback"), and the categories are keyword heuristics. Treat them as hints.
"""

import re
from typing import ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus

SERVICE_KEYS = (
    "vast_host.unit_loaded",
    "vast_host.service_active",
    "vast_host.active_state",
    "vast_host.sub_state",
    "vast_host.nrestarts",
    "vast_host.restart_loop",
)
LOG_KEYS = (
    "vast_host.log_lines_read",
    "vast_host.log_error_lines",
    "vast_host.log_error_network_lines",
    "vast_host.log_error_docker_lines",
    "vast_host.log_error_gpu_lines",
)

_ERROR = re.compile(r"\b(error|exception|traceback|critical|fatal)\b", re.IGNORECASE)
_NETWORK = re.compile(
    r"connect|timed out|timeout|unreachable|dns|resolv|socket", re.IGNORECASE
)
_DOCKER = re.compile(r"docker", re.IGNORECASE)
_GPU = re.compile(r"nvidia|nvml|\bgpu\b|cuda", re.IGNORECASE)
_TIME = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def parse_show(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            props[key.strip()] = value.strip()
    return props


def analyze_log(text: str) -> dict[str, int | str]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    found: dict[str, int | str] = {
        "lines": len(lines),
        "errors": 0,
        "network": 0,
        "docker": 0,
        "gpu": 0,
    }
    for line in lines:
        if not _ERROR.search(line):
            continue
        found["errors"] = int(found["errors"]) + 1
        for name, pattern in (
            ("network", _NETWORK),
            ("docker", _DOCKER),
            ("gpu", _GPU),
        ):
            if pattern.search(line):
                found[name] = int(found[name]) + 1
        stamp = _TIME.match(line)
        if stamp:
            found["last_error"] = f"{stamp.group(1)}T{stamp.group(2)}"
    return found


class VastHostCollector(Collector):
    name: ClassVar[str] = "vast_host"

    def collect(self, ctx: HostContext) -> list[Fact]:
        return [*self._service(ctx), *self._log(ctx)]

    # -- service --------------------------------------------------------------

    def _service(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("vast.service_show")
        source = result.source_command or None

        if not result.ok:
            text = f"{result.stdout}\n{result.stderr}".lower()
            if "not been booted with systemd" in text or "can't operate" in text:
                return [
                    self.fact(
                        key,
                        status=FactStatus.UNSUPPORTED,
                        source_command=source,
                        raw_excerpt="systemd is not running on this host",
                    )
                    for key in SERVICE_KEYS
                ]
            return [self.failed_from_result(key, result) for key in SERVICE_KEYS]

        props = parse_show(result.stdout)
        if not props:
            return [
                self.fact(
                    key,
                    status=FactStatus.ERROR,
                    source_command=source,
                    raw_excerpt="unparseable systemctl output",
                )
                for key in SERVICE_KEYS
            ]
        return self._service_facts(props, source)

    def _service_facts(self, props: dict[str, str], source: str | None) -> list[Fact]:
        facts: list[Fact] = []

        def missing(key: str) -> Fact:
            return self.fact(key, status=FactStatus.MISSING, source_command=source)

        load = props.get("LoadState")
        if load is None:
            facts.append(missing("vast_host.unit_loaded"))
        else:
            # A missing unit is not the same thing as a stopped one.
            facts.append(
                self.fact(
                    "vast_host.unit_loaded",
                    load == "loaded",
                    source_command=source,
                    raw_excerpt=None if load == "loaded" else f"LoadState={load}",
                )
            )

        active = props.get("ActiveState")
        sub = props.get("SubState")
        if active is None:
            facts += [
                missing("vast_host.service_active"),
                missing("vast_host.active_state"),
            ]
        else:
            facts += [
                self.fact(
                    "vast_host.service_active",
                    active == "active",
                    source_command=source,
                ),
                self.fact("vast_host.active_state", active, source_command=source),
            ]
        facts.append(
            self.fact("vast_host.sub_state", sub, source_command=source)
            if sub
            else missing("vast_host.sub_state")
        )

        raw_restarts = props.get("NRestarts")
        if raw_restarts is None:
            facts.append(missing("vast_host.nrestarts"))
        else:
            try:
                facts.append(
                    self.fact(
                        "vast_host.nrestarts", int(raw_restarts), source_command=source
                    )
                )
            except ValueError:
                facts.append(
                    self.fact(
                        "vast_host.nrestarts",
                        status=FactStatus.ERROR,
                        source_command=source,
                        raw_excerpt=f"unparseable: {raw_restarts[:30]}",
                    )
                )

        # `auto-restart` means the unit failed and systemd is waiting to start
        # it again: the restart loop the Vast offline page describes.
        if sub is None:
            facts.append(missing("vast_host.restart_loop"))
        else:
            facts.append(
                self.fact(
                    "vast_host.restart_loop",
                    sub == "auto-restart",
                    source_command=source,
                )
            )
        return facts

    # -- log ------------------------------------------------------------------

    def _log(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("vast.daemon_log_tail")
        source = result.source_command or None

        if result.status is CommandStatus.ERROR and "no such file" in (
            result.stderr.lower()
        ):
            # The daemon never wrote a log here (or it is somewhere else).
            return [
                self.fact(
                    key,
                    status=FactStatus.MISSING,
                    source_command=source,
                    raw_excerpt="kaalia.log not found",
                )
                for key in LOG_KEYS
            ]
        if not result.ok:
            return [self.failed_from_result(key, result) for key in LOG_KEYS]
        return self._log_facts(result, source)

    def _log_facts(self, result: CommandResult, source: str | None) -> list[Fact]:
        found = analyze_log(result.stdout)
        facts = [
            self.fact(
                "vast_host.log_lines_read", found["lines"], source_command=source
            ),
            self.fact(
                "vast_host.log_error_lines", found["errors"], source_command=source
            ),
            self.fact(
                "vast_host.log_error_network_lines",
                found["network"],
                source_command=source,
            ),
            self.fact(
                "vast_host.log_error_docker_lines",
                found["docker"],
                source_command=source,
            ),
            self.fact(
                "vast_host.log_error_gpu_lines", found["gpu"], source_command=source
            ),
        ]
        if "last_error" in found:
            facts.append(
                self.fact(
                    "vast_host.log_last_error_time",
                    str(found["last_error"]),
                    source_command=source,
                )
            )
        return facts
