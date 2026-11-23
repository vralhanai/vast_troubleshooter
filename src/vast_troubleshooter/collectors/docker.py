"""Docker daemon state and NVIDIA runtime registration, via `docker info`.

Only the trusted local endpoint is used (the runner pins it). Registering the
NVIDIA runtime does not prove a GPU container can start. That is a later,
opt-in check.
"""

import json
from typing import Any, ClassVar

from vast_troubleshooter.collectors.base import Collector
from vast_troubleshooter.context import HostContext
from vast_troubleshooter.models import Fact, FactStatus
from vast_troubleshooter.runner import CommandResult, CommandStatus

KEYS = (
    "docker.daemon_running",
    "docker.server_version",
    "docker.runtimes",
    "docker.nvidia_runtime_present",
    "docker.default_runtime",
    "docker.root_dir",
)


def _parse_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _server_errors(info: dict[str, Any] | None) -> str:
    errors = (info or {}).get("ServerErrors")
    if isinstance(errors, list):
        return " ".join(str(e) for e in errors)
    return ""


class DockerCollector(Collector):
    name: ClassVar[str] = "docker"

    def collect(self, ctx: HostContext) -> list[Fact]:
        result = ctx.runner.run("docker.info")
        source = result.source_command or None

        # Not run, or no answer: every fact is unknown, with the reason attached.
        if result.status in {
            CommandStatus.MISSING,
            CommandStatus.TIMEOUT,
            CommandStatus.REFUSED,
            CommandStatus.NEEDS_ROOT,
        }:
            return [self.failed_from_result(key, result) for key in KEYS]

        info = _parse_json(result.stdout)
        errors = _server_errors(info) or (result.stderr if not result.ok else "")
        if result.ok and info is not None and not errors:
            return self._running(info, source)
        return self._not_usable(result, info, errors, source)

    def _not_usable(
        self,
        result: CommandResult,
        info: dict[str, Any] | None,
        errors: str,
        source: str | None,
    ) -> list[Fact]:
        text = (errors or result.stderr or result.stdout).lower()
        if "permission denied" in text:
            # We could not ask. The daemon may be fine, so this is unknown.
            reason, running = "permission denied on the Docker socket", None
        elif "cannot connect" in text or "daemon running" in text:
            reason, running = "daemon is not running", False
        elif result.ok:
            reason, running = "unparseable docker info output", None
        else:
            return [self.failed_from_result(key, result) for key in KEYS]

        facts = [
            self.fact(
                "docker.daemon_running",
                running,
                status=FactStatus.OK if running is False else FactStatus.ERROR,
                source_command=source,
                raw_excerpt=reason,
            )
        ]
        facts += [
            self.fact(
                key, status=FactStatus.ERROR, source_command=source, raw_excerpt=reason
            )
            for key in KEYS[1:]
        ]
        return facts

    def _running(self, info: dict[str, Any], source: str | None) -> list[Fact]:
        facts = [self.fact("docker.daemon_running", True, source_command=source)]

        version = info.get("ServerVersion")
        facts.append(self._text("docker.server_version", version, source))

        runtimes = info.get("Runtimes")
        if isinstance(runtimes, dict):
            names = sorted(str(n) for n in runtimes)
            facts.append(
                self.fact("docker.runtimes", ",".join(names), source_command=source)
            )
            facts.append(
                self.fact(
                    "docker.nvidia_runtime_present",
                    "nvidia" in names,
                    source_command=source,
                )
            )
        else:
            for key in ("docker.runtimes", "docker.nvidia_runtime_present"):
                facts.append(
                    self.fact(key, status=FactStatus.MISSING, source_command=source)
                )

        facts.append(
            self._text("docker.default_runtime", info.get("DefaultRuntime"), source)
        )
        facts.append(self._text("docker.root_dir", info.get("DockerRootDir"), source))
        return facts

    def _text(self, key: str, value: object, source: str | None) -> Fact:
        if isinstance(value, str) and value:
            return self.fact(key, value, source_command=source)
        return self.fact(key, status=FactStatus.MISSING, source_command=source)
